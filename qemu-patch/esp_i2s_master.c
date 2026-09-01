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

/* i2s_reg.h: I2S_TX_CONF_REG is at +0x24, I2S_TX_START at bit 2. */
#define R_TX_CONF       0x24
#define TX_START        (1u << 2)

/* gdma_reg.h: the receive link's start is bit 22 and the transmit link's is 21.
 * They genuinely differ, and reading one with the other's layout finds a
 * channel that is not running. */
#define IN_LINK_START   (1u << 22)
#define OUT_LINK_START  (1u << 21)
#define LINK_ADDR       0x000FFFFF

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

/* Bounds on a transmit list; beyond these it is not a list. */
#define TX_MAX_DESCRIPTORS 64
#define TX_MAX_TRANSFER (256 * 1024)

struct EspI2sMaster {
    DeviceState parent_obj;

    MemoryRegion iomem;
    uint32_t base;
    uint32_t periph;            /* which peripheral the GDMA knows this as */
    char *name;

    ESPGdmaState *gdma;
    QEMUTimer *timer;

    uint32_t regs[I2S_REG_COUNT];
    bool rx_armed;
    bool tx_armed;
    uint32_t rx_cursor;         /* our way around the receive ring */
    uint32_t tx_head;           /* start of the queued transmit list */
    uint64_t buffers;
    uint64_t bytes;
    bool complained;
};

/* Find a channel bound to this block in the given direction and running.
 *
 * The link's START bit is a one-shot arm which the engine clears once it has
 * run, but the stream is still going -- what says so is I2S_RX_START or
 * I2S_TX_START, checked by the callers. So take the start bit as permission to
 * begin, and keep going afterwards until the stream is stopped.
 */
static bool i2s_find_channel(EspI2sMaster *s, int dir, bool *armed,
                             uint32_t *cursor, uint32_t *chan)
{
    const uint32_t start_bit = (dir == ESP_GDMA_IN_IDX) ? IN_LINK_START
                                                        : OUT_LINK_START;

    for (uint32_t i = 0; i < ESP32S3_GDMA_CHANNEL_COUNT; i++) {
        uint64_t sel = esp_gdma_read_chan_register(s->gdma, dir, i,
                                                   GDMA_PERI_SEL_REG);
        if ((sel & PERI_SEL_MASK) != s->periph) {
            continue;
        }
        uint64_t link = esp_gdma_read_chan_register(s->gdma, dir, i,
                                                    GDMA_LINK_REG);
        uint32_t addr = (uint32_t)(link & LINK_ADDR);
        if (addr == 0) {
            continue;
        }
        if (!(link & start_bit) && !*armed) {
            continue;
        }
        if (!*armed) {
            *cursor = DRAM_TOP_BITS | addr;
            *armed = true;
        }
        *chan = i;
        return true;
    }
    return false;
}

/* The descriptor the cursor is on, and how many bytes it is worth.
 *
 * One descriptor, never the chain: these are rings, so walking one looking for
 * an end does not terminate. A stream moves one buffer per period and the owner
 * bit is the handshake -- the engine clears it, the driver sets it again once
 * it has dealt with the buffer. Refusing one the CPU still owns is what the
 * hardware does and what stops a buffer being handled twice.
 *
 * Receiving, the interesting number is `size`, the room available. Transmitting
 * it is `length`, the bytes the driver actually put there.
 *
 * `suc_eof` means different things by direction, which matters a great deal.
 * Transmitting, it marks the last buffer of the list and the transfer ends
 * there. Receiving, the engine's own comment is that it only raises a status
 * bit and does not stop anything, because the ring keeps turning.
 */
static bool i2s_descriptor(uint32_t at, bool receiving, uint32_t *bytes,
                           uint32_t *next, bool *eof)
{
    uint8_t raw[DESC_BYTES];

    if (address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                           raw, sizeof(raw)) != MEMTX_OK) {
        return false;
    }
    uint32_t config = ldl_le_p(raw);
    uint32_t want = receiving ? (config & 0xFFF) : ((config >> 12) & 0xFFF);
    bool owner = (config >> 31) & 1;

    if (!owner || want == 0 || want > DESC_MAX_SIZE) {
        return false;
    }
    *bytes = want;
    *next = ldl_le_p(raw + 8);
    *eof = (config >> 30) & 1;
    return true;
}

/* Point the engine at the buffer being worked on. It always starts from the
 * link address and never advances it, whereas the hardware works its way around
 * the ring, so the cursor is ours to carry. Only the address is written: the
 * start bits would re-trigger a transfer. */
static void i2s_aim(EspI2sMaster *s, int dir, uint32_t chan, uint32_t cursor)
{
    esp_gdma_write_chan_register(s->gdma, dir, chan, GDMA_LINK_REG,
                                 cursor & LINK_ADDR);
}

static void i2s_receive(EspI2sMaster *s)
{
    uint32_t chan = 0, bytes = 0, next = 0;
    bool eof = false;

    if (!(s->regs[R_RX_CONF / 4] & RX_START)) {
        s->rx_armed = false;
        return;
    }
    if (!i2s_find_channel(s, ESP_GDMA_IN_IDX, &s->rx_armed, &s->rx_cursor,
                          &chan)) {
        return;
    }
    if (!i2s_descriptor(s->rx_cursor, true, &bytes, &next, &eof)) {
        /* The CPU still owns it. On real hardware that is an overrun and the
         * receiver stalls, which is what happens here too. */
        return;
    }

    i2s_aim(s, ESP_GDMA_IN_IDX, chan, s->rx_cursor);

    /* Silence is the honest sample value: there is no audio into this
     * emulator, and what the firmware waits for is the transfer. */
    g_autofree uint8_t *silence = g_malloc0(bytes);
    if (!esp_gdma_write_channel(s->gdma, chan, silence, bytes)) {
        if (!s->complained) {
            s->complained = true;
            warn_report("%s: GDMA refused %u receive bytes on channel %u",
                        s->name ? s->name : "i2s", bytes, chan);
        }
        return;
    }
    if (next) {
        s->rx_cursor = DRAM_TOP_BITS | (next & LINK_ADDR);
    }
    s->buffers++;
    s->bytes += bytes;
}

/* Total the queued transmit list, from its head to its end marker.
 *
 * Transmit is not receive turned round. `suc_eof` ends a transmit list, where on
 * receive it only raises a status bit and the ring keeps turning. And the
 * engine's read side is built to be handed a whole list at once -- it walks the
 * chain, clears the owner bits and raises EOF -- which is how the AES and SHA
 * models drive it. So the list is summed here and passed in one call.
 *
 * It has to be properly terminated to be touched at all. Stepping through it a
 * descriptor at a time, trusting each owner bit, walked straight past the
 * queued buffers and off the end of DRAM, clearing owner bits through
 * everything it crossed.
 */
static bool i2s_tx_total(uint32_t head, uint32_t *total)
{
    uint8_t raw[DESC_BYTES];
    uint32_t at = head;

    *total = 0;
    for (int i = 0; i < TX_MAX_DESCRIPTORS; i++) {
        if (address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                               raw, sizeof(raw)) != MEMTX_OK) {
            return false;
        }
        uint32_t config = ldl_le_p(raw);
        uint32_t length = (config >> 12) & 0xFFF;
        uint32_t next = ldl_le_p(raw + 8);

        if (!((config >> 31) & 1) || length > DESC_MAX_SIZE) {
            return false;               /* not ours, or not a descriptor */
        }
        *total += length;
        if (*total > TX_MAX_TRANSFER) {
            return false;
        }
        if ((config >> 30) & 1) {
            return *total != 0;         /* end marker */
        }
        if (next == 0 || next == at) {
            return false;               /* ran out without one */
        }
        at = next;
    }
    return false;
}

static void i2s_transmit(EspI2sMaster *s)
{
    uint32_t chan = 0;
    uint32_t total = 0;

    if (!(s->regs[R_TX_CONF / 4] & TX_START)) {
        s->tx_armed = false;
        return;
    }
    if (!i2s_find_channel(s, ESP_GDMA_OUT_IDX, &s->tx_armed, &s->tx_head,
                          &chan)) {
        return;
    }
    if (!i2s_tx_total(s->tx_head, &total)) {
        return;
    }

    i2s_aim(s, ESP_GDMA_OUT_IDX, chan, s->tx_head);

    /* The samples are read and dropped: there is nowhere for audio to go, and
     * the firmware waits for the transfer to finish, not for a sound. */
    g_autofree uint8_t *played = g_malloc(total);
    if (!esp_gdma_read_channel(s->gdma, chan, played, total)) {
        if (!s->complained) {
            s->complained = true;
            warn_report("%s: GDMA refused %u transmit bytes on channel %u",
                        s->name ? s->name : "i2s", total, chan);
        }
        return;
    }

    /* That list is done; the driver arms the link again for the next one. */
    s->tx_armed = false;
    s->buffers++;
    s->bytes += total;
}

static void i2s_tick(void *opaque)
{
    EspI2sMaster *s = opaque;

    if (!s->gdma) {
        return;
    }
    i2s_receive(s);
    i2s_transmit(s);
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
