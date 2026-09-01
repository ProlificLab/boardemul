/*
 * esp_i2s_master -- the receive half of an I2S controller, so a DMA capture
 * can complete.
 *
 * The T-Deck's vendor firmware starts a microphone capture during startup. It
 * is a capture and not a playback, which took a while to see: the registers it
 * drives are I2S_RX_*, the DMA direction is receive, and the block at
 * 0x6002D000 is I2S1 -- I2S0 is at 0x6000F000, and the board file had it
 * mislabelled.
 *
 * The fork already models GDMA: it walks descriptors, writes guest memory and
 * raises the interrupts. So this model is small. It keeps the I2S registers so
 * read-modify-write behaves (ADR 0008), watches I2S_RX_CONF for RX_START, and
 * then hands the DMA engine a buffer of silence whenever the next descriptor
 * in the ring belongs to the hardware. Silence is the honest sample value:
 * there is no audio into this emulator, and what the firmware waits for is the
 * transfer, not sound.
 *
 * Every register number and bit position here comes from Espressif's headers
 * and the ESP32-S3 technical reference manual. An earlier attempt inferred them
 * from bus traffic instead, got the direction wrong, and drained a transmit
 * channel that had never started -- clearing descriptor owner bits in whatever
 * guest memory the stale link happened to point at. ADR 0015 records both that
 * and what this model does and does not achieve.
 */

#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "qemu/log.h"
#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qom/object.h"
#include "hw/dma/esp_gdma.h"
#include "hw/dma/esp32s3_gdma.h"

#define TYPE_ESP_I2S_MASTER "esp-i2s-master"

OBJECT_DECLARE_SIMPLE_TYPE(EspI2sMaster, ESP_I2S_MASTER)

#define I2S_REG_COUNT (0x1000 / 4)

/* i2s_reg.h: I2S_RX_CONF_REG is at +0x20 and I2S_RX_START is bit 2. The
 * driver's last two writes before it waits are RX_UPDATE (bit 8), then this. */
#define R_RX_CONF       0x20
#define RX_START        (1u << 2)

/* gdma_reg.h: GDMA_INLINK_START_CHn is bit 22 -- the transmit side's is 21, and
 * the fork's own field definitions agree. */
#define IN_LINK_START   (1u << 22)
#define IN_LINK_ADDR    0x000FFFFF

/* GDMA_PERI_IN_SEL is six bits. */
#define PERI_SEL_MASK   0x3F

/* Descriptors live in the lower part of DRAM, which is why the link register
 * only carries twenty bits of address. */
#define DRAM_TOP_BITS   ((ESP_GDMA_RAM_ADDR >> 20) << 20)

/* How often the receiver looks for a buffer to fill. A real one delivers on
 * its sample clock; this is fast enough that a driver waiting on a buffer is
 * not visibly delayed, and costs nothing while no capture is running. */
#define I2S_POLL_NS (5 * 1000 * 1000)

/* Descriptor: size 0..11, length 12..23, suc_eof at 30, owner at 31. */
#define DESC_BYTES 12
#define DESC_MAX_SIZE 4092

struct EspI2sMaster {
    DeviceState parent_obj;

    MemoryRegion iomem;
    uint32_t base;
    uint32_t periph;            /* which peripheral the GDMA knows this as */
    char *name;

    ESPGdmaState *gdma;
    QEMUTimer *timer;

    uint32_t regs[I2S_REG_COUNT];
    bool armed;
    uint32_t cursor;            /* our way around the descriptor ring */
    uint64_t buffers;
    uint64_t bytes;
    bool complained;
};

/* Look at the descriptor the cursor is on.
 *
 * One descriptor, never the chain: a receive chain is a ring, so walking it
 * looking for an end runs until the loop guard stops it. A receiver fills one
 * buffer per period, and the owner bit is the handshake -- the engine clears it
 * once filled, the driver sets it again once it has taken the data. Refusing a
 * buffer the CPU still owns is both what the hardware does and what stops the
 * same buffer being delivered twice.
 *
 * `size` is the room in the buffer; `length` is what the hardware reports it
 * put there, so `size` is what says how much to deliver.
 */
static bool i2s_buffer_at(uint32_t at, uint32_t *capacity, uint32_t *next)
{
    uint8_t raw[DESC_BYTES];

    if (address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                           raw, sizeof(raw)) != MEMTX_OK) {
        return false;
    }
    uint32_t config = ldl_le_p(raw);
    uint32_t size = config & 0xFFF;
    bool owner = (config >> 31) & 1;

    if (!owner || size == 0 || size > DESC_MAX_SIZE) {
        return false;
    }
    *capacity = size;
    *next = ldl_le_p(raw + 8);
    return true;
}

static bool i2s_find_rx_channel(EspI2sMaster *s, uint32_t *chan)
{
    for (uint32_t i = 0; i < ESP32S3_GDMA_CHANNEL_COUNT; i++) {
        uint64_t sel = esp_gdma_read_chan_register(s->gdma, ESP_GDMA_IN_IDX, i,
                                                   GDMA_PERI_SEL_REG);
        if ((sel & PERI_SEL_MASK) != s->periph) {
            continue;
        }
        uint64_t link = esp_gdma_read_chan_register(s->gdma, ESP_GDMA_IN_IDX, i,
                                                    GDMA_LINK_REG);
        uint32_t addr = (uint32_t)(link & IN_LINK_ADDR);
        if (addr == 0) {
            continue;
        }
        /* The link's START bit is a one-shot arm, and the engine clears it once
         * it has run -- but the capture is still going, and what says so is
         * I2S_RX_START. So take the start bit as permission to begin, and keep
         * going afterwards until the capture is stopped. */
        if (!(link & IN_LINK_START) && !s->armed) {
            continue;
        }
        if (!s->armed) {
            s->cursor = DRAM_TOP_BITS | addr;
            s->armed = true;
        }
        *chan = i;
        return true;
    }
    return false;
}

static void i2s_deliver(EspI2sMaster *s)
{
    uint32_t chan = 0;
    uint32_t capacity = 0;
    uint32_t next = 0;

    if (!s->gdma || !(s->regs[R_RX_CONF / 4] & RX_START)) {
        /* Capture stopped; the next one arms itself again. */
        s->armed = false;
        return;
    }
    if (!i2s_find_rx_channel(s, &chan)) {
        return;
    }
    if (!i2s_buffer_at(s->cursor, &capacity, &next)) {
        /* The CPU still owns this one. On real hardware that is an overrun and
         * the receiver stalls, which is exactly what happens here. */
        return;
    }

    /* Point the engine at the buffer being filled. It always starts from the
     * link address and never advances it, whereas the hardware works its way
     * around the ring, so the cursor is ours to carry. Only the address is
     * written: the start bits would re-trigger a transfer. */
    esp_gdma_write_chan_register(s->gdma, ESP_GDMA_IN_IDX, chan, GDMA_LINK_REG,
                                 s->cursor & IN_LINK_ADDR);

    g_autofree uint8_t *silence = g_malloc0(capacity);
    if (!esp_gdma_write_channel(s->gdma, chan, silence, capacity)) {
        if (!s->complained) {
            s->complained = true;
            warn_report("%s: GDMA refused %u bytes on channel %u",
                        s->name ? s->name : "i2s", capacity, chan);
        }
        return;
    }

    if (next) {
        s->cursor = DRAM_TOP_BITS | (next & IN_LINK_ADDR);
    }
    s->buffers++;
    s->bytes += capacity;
}

static void i2s_tick(void *opaque)
{
    EspI2sMaster *s = opaque;

    i2s_deliver(s);
    timer_mod_ns(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + I2S_POLL_NS);
}

static uint64_t i2s_read(void *opaque, hwaddr addr, unsigned size)
{
    EspI2sMaster *s = opaque;

    return s->regs[(addr / 4) % I2S_REG_COUNT];
}

static void i2s_write(void *opaque, hwaddr addr, uint64_t value, unsigned size)
{
    EspI2sMaster *s = opaque;

    /* Stored rather than decoded (ADR 0008): returning zero for a register the
     * driver has just written breaks its read-modify-write, and this block is
     * full of them. Only RX_CONF is ever read back, in i2s_deliver(). */
    s->regs[(addr / 4) % I2S_REG_COUNT] = (uint32_t)value;
}

static const MemoryRegionOps i2s_ops = {
    .read = i2s_read,
    .write = i2s_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void i2s_realize(DeviceState *dev, Error **errp)
{
    EspI2sMaster *s = ESP_I2S_MASTER(dev);

    if (!s->base) {
        error_setg(errp, "esp-i2s-master: base is required");
        return;
    }

    /* Found rather than passed in, so the machine needs no edit to hand it
     * over. */
    Object *found = object_resolve_path_type("", TYPE_ESP_GDMA, NULL);
    if (!found) {
        error_setg(errp, "esp-i2s-master: no GDMA in this machine");
        return;
    }
    s->gdma = ESP_GDMA(found);

    memory_region_init_io(&s->iomem, OBJECT(s), &i2s_ops, s,
                          s->name ? s->name : "esp.i2s", 0x1000);
    memory_region_add_subregion_overlap(get_system_memory(), s->base,
                                        &s->iomem, 2);

    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, i2s_tick, s);
    timer_mod_ns(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + I2S_POLL_NS);
}

static Property i2s_props[] = {
    DEFINE_PROP_UINT32("base", EspI2sMaster, base, 0),
    /* Which peripheral the GDMA knows this block as: 3 is I2S0, 4 is I2S1.
     * The board says which rather than this file assuming. */
    DEFINE_PROP_UINT32("periph", EspI2sMaster, periph, GDMA_I2S1),
    DEFINE_PROP_STRING("name", EspI2sMaster, name),
    DEFINE_PROP_END_OF_LIST(),
};

static void i2s_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = i2s_realize;
    dc->user_creatable = true;
    device_class_set_props(dc, i2s_props);
}

static const TypeInfo i2s_info = {
    .name = TYPE_ESP_I2S_MASTER,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspI2sMaster),
    .class_init = i2s_class_init,
};

static void i2s_register_types(void)
{
    type_register_static(&i2s_info);
}

type_init(i2s_register_types)
