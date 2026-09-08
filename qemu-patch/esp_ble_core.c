/*
 * esp_ble_core.c -- the ESP32-S3 Bluetooth link-layer core's register page
 * (0x60031000), on QEMU's own clock.
 *
 * Nothing documents this page. Everything here was read off the ROM link
 * layer's code and the controller blob's dispatcher, one check at a time
 * (ADR 0024), and prototyped in Python first. It moved to C for one reason
 * (ADR 0025): the scheduler arms a timer for the very half-slot its event
 * starts in, and its event-start routine (ROM 0x4002ccd8) cancels the event
 * unless the interrupt is handled within that half-slot, 312.5 us. A timer
 * fired from Python through a socket is late by construction; a QEMU timer on
 * the virtual clock fires at the target, and the guest samples the clock
 * microseconds later, inside the same half-slot, as it does on the silicon.
 *
 * The registers, with where each was read:
 *
 *   +0x000 RWBLECNTL   bit 31 a reset the core clears itself (blob
 *                      0x42145bac spins `extui 31,1; bnez`); bit 27 the
 *                      request for a software interrupt (ROM 0x4002c330..74:
 *                      ack bit 12, enable it, set bit 27), raised as status
 *                      bit 12 and cleared itself.
 *   +0x004 VERSION     0x09001b00, asserted on at lld.c:318; the ROM's
 *                      routines for this core are named rw_v9.
 *   +0x00C INTCNTL     the enable mask.
 *   +0x010 INTSTAT     pending & mask.
 *   +0x014 INTRAWSTAT  pending.
 *   +0x018 INTACK      write one to clear (r_rwip_isr, ROM 0x4002c37c).
 *   +0x01C SLOTCLK     bit 31 samples the clock and clears itself; the low
 *                      28 bits then read the count in half-slots
 *                      (r_rwip_time_get, ROM 0x4002bd50).
 *   +0x020 FINETIMECNT the sampled half-microseconds into the half-slot,
 *                      0..624 (r_rwip_timer_hus_set stores 624 - hus).
 *   +0x0E4 target, 10 ms units, status bit 9    (r_rwip_timer_10ms_set)
 *   +0x0E8 target, half-slots, status bit 10    (r_rwip_timer_hs_set)
 *   +0x0EC target, half-slots, with the fine part at +0x0F0 as 624 - hus,
 *          status bit 11 (r_rwip_timer_hus_set; the bit its setter
 *          acknowledges and enables, watched live)
 *   +0x100 the kick (r_sch_prog_ble_push, ROM 0x4002d674, its last store):
 *          bit 31 with the index of an exchange-table entry the link layer
 *          has just filled. The table is exchange-memory offset 0, so it lives
 *          in RAM where base register 0 (+0x204) points: the register's low
 *          eighteen bits, shifted left by two, under 0x3FC00000
 *          (r_emi_get_mem_addr_by_offset, ROM 0x4000a360). An entry is 16
 *          bytes: bits 5..3 of the first halfword are the status the core
 *          writes back, the start time is at +2 (low sixteen bits of the
 *          half-slot count) and +4 (the high twelve), the fine count at +6
 *          (r_sch_prog_push, ROM 0x4002d80c). At the end of the event the
 *          core sets the status to 3 and raises status bit 5, which the
 *          dispatcher hands to the end handler; r_sch_prog_end_isr (ROM
 *          0x4002d58c) runs the event's callback only for a status of 3..5.
 *          An entry whose time has passed is skipped: status 4, bit 6.
 *          Nothing here transmits; the event is its own end.
 *   +0x2D8 the dispatcher's FIFO (blob 0x4037ce70..9b): bits 9..5 count the
 *          entries, bits 31..10 carry the head entry's status word, a write
 *          of bit 0 pops it; the configuration bits the blob wrote at init
 *          (bit 31 and 0x1e) read back unchanged.
 *   everything else, the exchange-memory base registers at +0x204 and up
 *          included, keeps what was written: emi.c:164 only checks that.
 *
 * The line goes to the RWBLE interrupt source; the controller mapped it to
 * a CPU interrupt, and its handler there is the dispatcher that drains the
 * FIFO into r_rwip_isr, which acknowledges. The level is high while anything
 * is pending under the mask or an entry waits in the FIFO.
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

#define TYPE_ESP_BLE_CORE "esp-ble-core"
OBJECT_DECLARE_SIMPLE_TYPE(EspBleCore, ESP_BLE_CORE)

#define R_CNTL          0x000
#define R_VERSION       0x004
#define R_INTCNTL       0x00C
#define R_INTSTAT       0x010
#define R_INTRAWSTAT    0x014
#define R_INTACK        0x018
#define R_SLOTCLK       0x01C
#define R_FINETIMECNT   0x020
#define R_TGT_10MS      0x0E4
#define R_TGT_HS        0x0E8
#define R_TGT_HUS_HS    0x0EC
#define R_TGT_HUS_FINE  0x0F0
#define R_ET_KICK       0x100
#define R_EM_BASE0      0x204
#define R_INTFIFO       0x2D8

#define CNTL_RESET      (1u << 31)
#define CNTL_SWINT_REQ  (1u << 27)
#define SLOTCLK_SAMP    (1u << 31)
#define SLOTCLK_COUNT   0x0FFFFFFFu
#define VERSION_RW_V9   0x09001B00u
#define BIT_10MS        (1u << 9)
#define BIT_HS          (1u << 10)
#define BIT_HUS         (1u << 11)
#define BIT_SWINT       (1u << 12)
#define FIFO_CONFIG     0x8000001Eu
#define FIFO_DEPTH      16
#define BIT_END         (1u << 5)
#define BIT_SKIP        (1u << 6)

#define ET_ENTRY_SIZE   16
#define ET_STATUS_SHIFT 3
#define ET_STATUS_MASK  (7u << ET_STATUS_SHIFT)
#define ET_ENDED        3
#define ET_SKIPPED      4
#define ET_MAX_PENDING  16
#define EVENT_HUS       4000       /* 2 ms: three advertising PDUs and their windows */

#define HUS_PER_HS      625        /* half-microseconds in a half-slot */
#define NS_PER_HUS      500        /* a half-microsecond */
#define REG_COUNT       1024
#define PAGE_SIZE       0x1000

struct EspBleCore {
    DeviceState parent_obj;
    MemoryRegion iomem;
    char *intc_type;
    uint64_t base;
    int32_t priority;
    int32_t irq_source;

    uint32_t regs[REG_COUNT];   /* what the guest wrote, word by word */
    uint32_t raw;               /* pending status bits */
    uint32_t slot, fine;        /* the last clock sample */
    uint32_t fifo[FIFO_DEPTH];
    unsigned fifo_len;

    /* Exchange-table entries kicked and not yet ended, by start time. */
    struct { unsigned idx; int64_t start_hus; } events[ET_MAX_PENDING];
    unsigned n_events;
    QEMUTimer *event_timer;

    QEMUTimer *timer;
    qemu_irq irq_line;
    int irq_level;
    QEMUBH *irq_bh;
};

/* -- time ---------------------------------------------------------------- */

static int64_t ble_now_hus(void)
{
    return qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / NS_PER_HUS;
}

/* The due time of one timer in half-microseconds, or -1 if it is not
 * armed. A target already raised does not count until it is re-armed, so an
 * acknowledged timer whose target stays in the past fires once. */
static int64_t ble_due(EspBleCore *s, uint32_t bit)
{
    const uint32_t mask = s->regs[R_INTCNTL / 4];

    if (!(mask & bit) || (s->raw & bit)) {
        return -1;
    }
    switch (bit) {
    case BIT_10MS:
        return ((int64_t)s->regs[R_TGT_10MS / 4] << 5) * HUS_PER_HS;
    case BIT_HS:
        return (int64_t)s->regs[R_TGT_HS / 4] * HUS_PER_HS;
    case BIT_HUS:
        return (int64_t)s->regs[R_TGT_HUS_HS / 4] * HUS_PER_HS
               + (624 - (s->regs[R_TGT_HUS_FINE / 4] & 0xFFFF));
    default:
        return -1;
    }
}

/* -- the line ------------------------------------------------------------ */

static void ble_irq_bh(void *opaque)
{
    EspBleCore *s = opaque;
    int level = ((s->raw & s->regs[R_INTCNTL / 4]) || s->fifo_len) ? 1 : 0;

    if (!s->irq_line || level == s->irq_level) {
        return;
    }
    s->irq_level = level;
    qemu_set_irq(s->irq_line, level);
}

/* Deferred to a bottom half for the same reason as the I2C master's: raising
 * the line inside the guest's own write would deliver the interrupt on the
 * next instruction, before the setter has finished. */
static void ble_update_irq(EspBleCore *s)
{
    if (s->irq_bh) {
        qemu_bh_schedule(s->irq_bh);
    }
}

static void ble_fifo_push(EspBleCore *s, uint32_t status)
{
    if (s->fifo_len < FIFO_DEPTH) {
        s->fifo[s->fifo_len++] = status;
    } else {
        /* The dispatcher drains it as fast as it fills; a full FIFO means
         * something else is wrong, and dropping the word says so. */
        error_report("esp-ble-core: interrupt FIFO full, dropping 0x%x", status);
    }
}

/* Raise what is due, arm the timer for the earliest of the rest. */
static void ble_rearm(EspBleCore *s)
{
    static const uint32_t bits[] = { BIT_10MS, BIT_HS, BIT_HUS };
    const int64_t now = ble_now_hus();
    int64_t next = -1;
    uint32_t raised = 0;

    for (unsigned i = 0; i < ARRAY_SIZE(bits); i++) {
        int64_t due = ble_due(s, bits[i]);
        if (due < 0) {
            continue;
        }
        if (due <= now) {
            s->raw |= bits[i];
            raised |= bits[i];
        } else if (next < 0 || due < next) {
            next = due;
        }
    }
    if (raised) {
        ble_fifo_push(s, raised);
        ble_update_irq(s);
    }
    if (next >= 0) {
        timer_mod_ns(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                               + (next - now) * NS_PER_HUS);
    } else {
        timer_del(s->timer);
    }
}

static void ble_expire(void *opaque)
{
    ble_rearm(opaque);
}

/* -- the exchange table ----------------------------------------------------- */

static hwaddr ble_et_entry(EspBleCore *s, unsigned idx)
{
    hwaddr base = 0x3FC00000u | ((s->regs[R_EM_BASE0 / 4] & 0x3FFFFu) << 2);
    return base + (hwaddr)idx * ET_ENTRY_SIZE;
}

static void ble_arm_event_timer(EspBleCore *s)
{
    if (!s->n_events) {
        timer_del(s->event_timer);
        return;
    }
    int64_t end = s->events[0].start_hus + EVENT_HUS;
    int64_t now = ble_now_hus();
    timer_mod_ns(s->event_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                 + (end > now ? (end - now) : 0) * NS_PER_HUS);
}

/* The head event is over: write its status into the entry, tell the link
 * layer, and move on to the next. */
static void ble_event_done(void *opaque)
{
    EspBleCore *s = opaque;
    if (!s->n_events) {
        return;
    }
    unsigned idx = s->events[0].idx;
    bool late = s->events[0].start_hus + EVENT_HUS < ble_now_hus() - EVENT_HUS;
    hwaddr entry = ble_et_entry(s, idx);
    uint16_t word0 = 0;

    address_space_read(&address_space_memory, entry, MEMTXATTRS_UNSPECIFIED, &word0, 2);
    word0 = (word0 & ~ET_STATUS_MASK) | ((late ? ET_SKIPPED : ET_ENDED) << ET_STATUS_SHIFT);
    address_space_write(&address_space_memory, entry, MEMTXATTRS_UNSPECIFIED, &word0, 2);
    qemu_log_mask(LOG_UNIMP, "esp-ble-core: entry %u %s at %lld hus%c", idx,
                  late ? "skipped" : "ended", (long long)ble_now_hus(), 10);

    memmove(&s->events[0], &s->events[1], (s->n_events - 1) * sizeof(s->events[0]));
    s->n_events--;
    s->raw |= late ? BIT_SKIP : BIT_END;
    ble_fifo_push(s, late ? BIT_SKIP : BIT_END);
    ble_update_irq(s);
    ble_arm_event_timer(s);
}

static void ble_kick(EspBleCore *s, uint32_t value)
{
    unsigned idx = value & 0xF;
    uint8_t e[ET_ENTRY_SIZE];
    hwaddr entry = ble_et_entry(s, idx);

    if (s->n_events >= ET_MAX_PENDING) {
        error_report("esp-ble-core: exchange table full, entry %u dropped", idx);
        return;
    }
    address_space_read(&address_space_memory, entry, MEMTXATTRS_UNSPECIFIED, e, sizeof(e));
    uint32_t ts_hs = (uint32_t)(e[2] | (e[3] << 8)) | ((uint32_t)((e[4] | (e[5] << 8)) & 0xFFF) << 16);
    uint32_t hus = (e[6] | (e[7] << 8)) & 0xFFFF;
    int64_t start = (int64_t)ts_hs * HUS_PER_HS + hus;
    /* The whole entry: four bytes of it are understood (a word of flags
     * and the start time), and the rest is unread. Whatever tells the
     * baseband which packet to send is in there. */
    qemu_log_mask(LOG_UNIMP, "esp-ble-core: kick entry %u at 0x%08llx: start %lld hus, now %lld hus, entry "
                  "%02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x%c",
                  idx, (unsigned long long)entry, (long long)start,
                  (long long)ble_now_hus(),
                  e[0], e[1], e[2], e[3], e[4], e[5], e[6], e[7],
                  e[8], e[9], e[10], e[11], e[12], e[13], e[14], e[15], 10);

    unsigned i = s->n_events;
    while (i > 0 && s->events[i - 1].start_hus > start) {
        s->events[i] = s->events[i - 1];
        i--;
    }
    s->events[i].idx = idx;
    s->events[i].start_hus = start;
    s->n_events++;
    ble_arm_event_timer(s);
}

/* -- registers ----------------------------------------------------------- */

static uint32_t ble_read_word(EspBleCore *s, hwaddr offset)
{
    switch (offset) {
    case R_VERSION:
        return VERSION_RW_V9;
    case R_INTSTAT:
        return s->raw & s->regs[R_INTCNTL / 4];
    case R_INTRAWSTAT:
        return s->raw;
    case R_SLOTCLK:
        return (s->regs[R_SLOTCLK / 4] & 0x70000000u) | (s->slot & SLOTCLK_COUNT);
    case R_FINETIMECNT:
        return s->fine;
    case R_INTFIFO: {
        uint32_t v = s->regs[R_INTFIFO / 4] & FIFO_CONFIG;
        if (s->fifo_len) {
            v |= (MIN(s->fifo_len, 31u) << 5) | ((s->fifo[0] & 0x1FFFFFu) << 10);
        }
        return v;
    }
    default:
        return s->regs[offset / 4];
    }
}

static void ble_write_word(EspBleCore *s, hwaddr offset, uint32_t value)
{
    switch (offset) {
    case R_CNTL:
        if (value & CNTL_SWINT_REQ) {
            s->raw |= BIT_SWINT;
            ble_fifo_push(s, BIT_SWINT);
            ble_update_irq(s);
        }
        s->regs[offset / 4] = value & ~(CNTL_RESET | CNTL_SWINT_REQ);
        return;
    case R_SLOTCLK:
        if (value & SLOTCLK_SAMP) {
            int64_t hus = ble_now_hus();
            s->slot = (uint32_t)(hus / HUS_PER_HS) & SLOTCLK_COUNT;
            s->fine = (uint32_t)(hus % HUS_PER_HS);
        }
        s->regs[offset / 4] = value & ~SLOTCLK_SAMP;
        return;
    case R_INTACK:
        s->raw &= ~value;
        ble_update_irq(s);
        return;
    case R_INTFIFO:
        if (value & 1) {
            if (s->fifo_len) {
                memmove(&s->fifo[0], &s->fifo[1], (s->fifo_len - 1) * sizeof(s->fifo[0]));
                s->fifo_len--;
            }
            ble_update_irq(s);
            return;
        }
        s->regs[offset / 4] = value;
        return;
    case R_ET_KICK:
        s->regs[offset / 4] = value;
        if (value & 0x80000000u) {
            ble_kick(s, value);
        }
        return;
    case R_INTCNTL:
    case R_TGT_10MS:
    case R_TGT_HS:
    case R_TGT_HUS_HS:
    case R_TGT_HUS_FINE:
        s->regs[offset / 4] = value;
        ble_rearm(s);
        ble_update_irq(s);
        return;
    default:
        s->regs[offset / 4] = value;
        return;
    }
}

static uint64_t ble_read(void *opaque, hwaddr offset, unsigned size)
{
    EspBleCore *s = opaque;
    uint32_t word = ble_read_word(s, offset & ~3);
    return (word >> ((offset & 3) * 8)) & (size == 4 ? 0xFFFFFFFFu : ((1u << (size * 8)) - 1));
}

static void ble_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    EspBleCore *s = opaque;
    hwaddr aligned = offset & ~3;

    /* Which exchange-memory pages the controller maps, and to where. The ROM
     * turns an offset into an address by reading one of these and checking its
     * top bits against the page it asked for; a page it never mapped fails
     * that check, and whatever it was staging goes nowhere useful. */
    if (aligned >= R_EM_BASE0 && aligned < R_EM_BASE0 + 64 * 4) {
        qemu_log_mask(LOG_UNIMP, "esp-ble-core: em base %u = 0x%08x "
                      "(page 0x%04x, ram 0x%08x)%c",
                      (unsigned)((aligned - R_EM_BASE0) / 4), (uint32_t)value,
                      (uint32_t)(((value >> 18) & 0x3FFF) << 2),
                      0x3FC00000u | (uint32_t)((value & 0x3FFFF) << 2), 10);
    }
    uint32_t word = (uint32_t)value;

    if (size < 4) {
        /* The link layer uses word accesses everywhere seen; a narrower one
         * merges into what the register holds, so nothing is lost. */
        uint32_t shift = (offset & 3) * 8;
        uint32_t mask = ((1u << (size * 8)) - 1) << shift;
        word = (s->regs[aligned / 4] & ~mask) | (((uint32_t)value << shift) & mask);
    }
    ble_write_word(s, aligned, word);
}

static const MemoryRegionOps ble_ops = {
    .read = ble_read,
    .write = ble_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

/* -- the device ---------------------------------------------------------- */

static void ble_realize(DeviceState *dev, Error **errp)
{
    EspBleCore *s = ESP_BLE_CORE(dev);

    if (s->irq_source >= 0) {
        Object *intc = object_resolve_path_type("", s->intc_type, NULL);
        if (!intc) {
            error_setg(errp, "esp-ble-core: no '%s' in the machine", s->intc_type);
            return;
        }
        s->irq_line = qdev_get_gpio_in(DEVICE(intc), s->irq_source);
        s->irq_bh = qemu_bh_new(ble_irq_bh, s);
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ble_expire, s);
    s->event_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ble_event_done, s);

    memory_region_init_io(&s->iomem, OBJECT(s), &ble_ops, s, TYPE_ESP_BLE_CORE, PAGE_SIZE);
    memory_region_add_subregion_overlap(get_system_memory(), s->base, &s->iomem, s->priority);
}

static Property ble_props[] = {
    DEFINE_PROP_STRING("intc", EspBleCore, intc_type),
    DEFINE_PROP_UINT64("base", EspBleCore, base, 0x60031000),
    DEFINE_PROP_INT32("priority", EspBleCore, priority, 100),
    DEFINE_PROP_INT32("irq", EspBleCore, irq_source, 8),
    DEFINE_PROP_END_OF_LIST(),
};

static void ble_instance_init(Object *obj)
{
    EspBleCore *s = ESP_BLE_CORE(obj);
    s->intc_type = g_strdup("misc.esp32s3.intmatrix");
    s->irq_level = 0;
}

static void ble_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    dc->realize = ble_realize;
    device_class_set_props(dc, ble_props);
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
}

static const TypeInfo ble_info = {
    .name = TYPE_ESP_BLE_CORE,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspBleCore),
    .instance_init = ble_instance_init,
    .class_init = ble_class_init,
};

static void ble_register_types(void)
{
    type_register_static(&ble_info);
}

type_init(ble_register_types)
