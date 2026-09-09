/*
 * esp_wifi_mac.c -- the WiFi MAC's register page (0x60033000), and a way to
 * put a frame in front of the driver.
 *
 * This window was a trace region: it remembered what was written and answered
 * reads with it, which is enough for the driver to start its station and scan
 * (ADR 0054) and not enough for it ever to hear anything. Everything here
 * keeps that behaviour; what it adds is the ability to hand the driver a
 * received frame, which needs writing into guest memory and raising an
 * interrupt, and neither is possible from the register-level bridge.
 *
 * What is known, and where from:
 *
 *   +0x088  the receive ring. The driver writes one internal-RAM address into
 *           this page while starting, and it is a chain of descriptors.
 *   the descriptor: twelve bytes, {word, buffer, next}, and the word is the
 *           part's ordinary DMA descriptor -- size in bits 0..11, length in
 *           12..23, suc_eof at 30 and owner at 31, the same layout
 *           esp_i2s_master.c already walks. Observed as 0x808a48a4: a buffer
 *           of 2212 bytes, a length preset to the same, owner set.
 *
 * What is not known is what the driver does when it is given one, and which
 * interrupt tells it to look. Both are unanswerable from a run in which no
 * frame ever arrives, so this device exists to end that: it delivers on
 * demand, under BOARDEMUL_WIFI_FRAME, and reports whether the driver took the
 * descriptor back. A descriptor the driver has processed is one it re-arms by
 * setting the owner bit again, so that is the signal to watch for, and it is
 * the same whether the frame was understood or discarded.
 *
 * Nothing here is on by default. With the hatch unset this device is the trace
 * region it replaces, byte for byte.
 */

#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "qemu/timer.h"
#include "qemu/main-loop.h"
#include "qemu/log.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "hw/irq.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qapi/error.h"
#include "qom/object.h"
#include "sysemu/reset.h"

#define TYPE_ESP_WIFI_MAC "esp-wifi-mac"
OBJECT_DECLARE_SIMPLE_TYPE(EspWifiMac, ESP_WIFI_MAC)

#define PAGE_SIZE       0x1000
#define REG_COUNT       (PAGE_SIZE / 4)
/* Where the driver puts the receive ring, read off a run (ADR 0054). */
#define R_RX_RING       0x088
/* This page is not blank paper. Before this device existed it was served by
 * the board's PhyGates model, which answers the waits the firmware makes
 * during bring-up -- each one read off a disassembled polling loop, never
 * guessed (ADR 0019). One of them is here: the driver writes to +0xd14 and
 * then waits on bit 0 for the MAC to acknowledge its reset. Answering it is
 * not decoration; without it the station never starts, which is exactly what
 * happened when this device first replaced the window.
 *
 * BOARDEMUL_PHY_GATES=0 turns it off, the same as on the Python side, because
 * that is how the gate was found in the first place.
 */
#define R_MAC_RESET     0xd14
#define MAC_RESET_ACK   0x00000001u

/* The part's ordinary DMA descriptor, as esp_i2s_master.c documents it. */
#define DESC_SIZE_MASK    0xFFFu
#define DESC_LENGTH_SHIFT 12
#define DESC_OWNER        (1u << 31)
#define DESC_EOF          (1u << 30)
#define DESC_BYTES        12

/* How far to follow a chain before deciding it is not one. */
#define DESC_MAX          16

static const uint8_t beacon[] = {
    0x80, 0x00,                                     /* a beacon */
    0x00, 0x00,                                     /* duration */
    0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,             /* to everyone */
    0x02, 0x00, 0x00, 0x00, 0x00, 0x01,             /* from us */
    0x02, 0x00, 0x00, 0x00, 0x00, 0x01,             /* our network */
    0x00, 0x00,                                     /* sequence */
    0, 0, 0, 0, 0, 0, 0, 0,                         /* timestamp */
    0x64, 0x00,                                     /* every 100 */
    0x01, 0x00,                                     /* an infrastructure one */
    0x00, 0x09, 'B', 'o', 'a', 'r', 'd', 'E', 'm', 'u', 'l',
    0x01, 0x04, 0x82, 0x84, 0x8B, 0x96,             /* the rates it offers */
    0x03, 0x01, 0x01,                               /* on channel one */
};

struct EspWifiMac {
    DeviceState parent_obj;
    MemoryRegion iomem;

    char *intc_type;
    uint64_t base;
    int32_t priority;
    int32_t irq_source;

    uint32_t regs[REG_COUNT];
    qemu_irq irq_line;
    int irq_level;

    QEMUTimer *hatch;
    bool gated;
    uint32_t ring;              /* what the driver handed us */
    hwaddr delivered_to;        /* the descriptor a frame went into */
    unsigned deliveries;
};

static void wifi_raise(EspWifiMac *s, int level)
{
    if (s->irq_line && level != s->irq_level) {
        s->irq_level = level;
        qemu_set_irq(s->irq_line, level);
    }
}

/* Follow the chain to the first descriptor the hardware still owns, which is
 * the one a real MAC would fill next. */
static hwaddr wifi_free_descriptor(EspWifiMac *s, uint32_t *word_out)
{
    hwaddr at = s->ring;

    for (unsigned i = 0; i < DESC_MAX && at; i++) {
        uint32_t desc[3] = { 0 };
        if (address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                               desc, sizeof(desc)) != MEMTX_OK) {
            return 0;
        }
        if (desc[0] & DESC_OWNER) {
            *word_out = desc[0];
            return at;
        }
        at = desc[2];
    }
    return 0;
}

/* Did the driver take the descriptor we filled? It says so by owning it
 * again, which it does whether or not it made anything of the contents. */
static void wifi_check_taken(EspWifiMac *s)
{
    uint32_t desc[3] = { 0 };

    if (!s->delivered_to) {
        return;
    }
    if (address_space_read(&address_space_memory, s->delivered_to,
                           MEMTXATTRS_UNSPECIFIED, desc, sizeof(desc)) != MEMTX_OK) {
        return;
    }
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: descriptor 0x%08llx now 0x%08x -- "
                  "the driver has %s it%c", (unsigned long long)s->delivered_to,
                  desc[0], (desc[0] & DESC_OWNER) ? "taken" : "NOT taken", 10);
}

static void wifi_deliver(void *opaque)
{
    EspWifiMac *s = opaque;
    uint32_t word = 0;
    hwaddr desc;

    if (s->delivered_to) {
        wifi_check_taken(s);
        wifi_raise(s, 0);
        return;
    }
    if (!s->ring) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: no receive ring was ever "
                      "handed over; nothing to deliver into%c", 10);
        return;
    }
    desc = wifi_free_descriptor(s, &word);
    if (!desc) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the ring at 0x%08x has no "
                      "descriptor the hardware owns%c", s->ring, 10);
        return;
    }

    uint32_t buffer = 0;
    address_space_read(&address_space_memory, desc + 4, MEMTXATTRS_UNSPECIFIED,
                       &buffer, sizeof(buffer));
    if (!buffer) {
        return;
    }
    address_space_write(&address_space_memory, buffer, MEMTXATTRS_UNSPECIFIED,
                        beacon, sizeof(beacon));
    /* Keep the size the driver gave the buffer; say how much arrived, mark it
     * the end of the frame, and hand it back by clearing the owner bit. */
    uint32_t filled = (word & DESC_SIZE_MASK)
                    | ((uint32_t)sizeof(beacon) << DESC_LENGTH_SHIFT)
                    | DESC_EOF;
    address_space_write(&address_space_memory, desc, MEMTXATTRS_UNSPECIFIED,
                        &filled, sizeof(filled));
    s->delivered_to = desc;
    s->deliveries++;
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: put %u byte(s) into the buffer at "
                  "0x%08x of descriptor 0x%08llx, word 0x%08x -> 0x%08x, and "
                  "raised interrupt %d%c", (unsigned)sizeof(beacon), buffer,
                  (unsigned long long)desc, word, filled, s->irq_source, 10);
    wifi_raise(s, 1);
    /* Look again shortly: the answer is whether the driver re-armed it. */
    timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 200000000LL);
}

static uint64_t wifi_read(void *opaque, hwaddr offset, unsigned size)
{
    EspWifiMac *s = opaque;
    uint32_t value = offset < PAGE_SIZE ? s->regs[offset / 4] : 0;

    if (s->gated && offset == R_MAC_RESET) {
        value |= MAC_RESET_ACK;
    }
    return value;
}

static void wifi_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    EspWifiMac *s = opaque;

    if (offset >= PAGE_SIZE) {
        return;
    }
    s->regs[offset / 4] = (uint32_t)value;
    if (offset == R_RX_RING && value) {
        s->ring = (uint32_t)value;
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the driver handed over a "
                      "receive ring at 0x%08x%c", s->ring, 10);
    }
}

static const MemoryRegionOps wifi_ops = {
    .read = wifi_read,
    .write = wifi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    /* The handlers above deal in whole registers, so QEMU is asked to turn a
     * narrower access into one -- read-modify-write for a byte written, a
     * field extracted for a byte read. Handling them as words directly loses
     * the rest of the register, and the driver stopped bringing its station up
     * when this said one. */
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

/* These are registers inside the chip, so the machine's reset clears them --
 * the same as the window this replaces. It has to be registered by hand: this
 * device has no parent bus, so the resettable phases never fire (ADR 0035). */
static void wifi_reset(void *opaque)
{
    EspWifiMac *s = opaque;
    memset(s->regs, 0, sizeof(s->regs));
    s->ring = 0;
    s->delivered_to = 0;
    wifi_raise(s, 0);
}

static void wifi_realize(DeviceState *dev, Error **errp)
{
    EspWifiMac *s = ESP_WIFI_MAC(dev);
    const char *after = getenv("BOARDEMUL_WIFI_FRAME");

    if (s->irq_source >= 0) {
        Object *intc = object_resolve_path_type("", s->intc_type, NULL);
        if (!intc) {
            error_setg(errp, "esp-wifi-mac: no '%s' in the machine", s->intc_type);
            return;
        }
        s->irq_line = qdev_get_gpio_in(DEVICE(intc), s->irq_source);
    }
    s->gated = g_strcmp0(getenv("BOARDEMUL_PHY_GATES"), "0") != 0;
    s->hatch = timer_new_ns(QEMU_CLOCK_VIRTUAL, wifi_deliver, s);
    qemu_register_reset(wifi_reset, s);
    if (after) {
        /* Investigation hatch: how many seconds into the run to deliver one.
         * Off unless asked for, and then it does exactly one thing once. */
        timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                     + (int64_t)(atof(after) * 1000000000.0));
    }

    memory_region_init_io(&s->iomem, OBJECT(s), &wifi_ops, s,
                          TYPE_ESP_WIFI_MAC, PAGE_SIZE);
    memory_region_add_subregion_overlap(get_system_memory(), s->base,
                                        &s->iomem, s->priority);
}

static Property wifi_props[] = {
    DEFINE_PROP_STRING("intc", EspWifiMac, intc_type),
    DEFINE_PROP_UINT64("base", EspWifiMac, base, 0x60033000),
    DEFINE_PROP_INT32("priority", EspWifiMac, priority, 100),
    /* The WiFi MAC's own source number. Which one the driver actually listens
     * on is not settled, so it is a property rather than a constant. */
    DEFINE_PROP_INT32("irq", EspWifiMac, irq_source, 0),
    DEFINE_PROP_END_OF_LIST(),
};

static void wifi_instance_init(Object *obj)
{
    EspWifiMac *s = ESP_WIFI_MAC(obj);
    s->intc_type = g_strdup("misc.esp32s3.intmatrix");
    s->irq_level = 0;
}

static void wifi_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = wifi_realize;
    device_class_set_props(dc, wifi_props);
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
}

static const TypeInfo wifi_info = {
    .name = TYPE_ESP_WIFI_MAC,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspWifiMac),
    .instance_init = wifi_instance_init,
    .class_init = wifi_class_init,
};

static void wifi_register_types(void)
{
    type_register_static(&wifi_info);
}

type_init(wifi_register_types)
