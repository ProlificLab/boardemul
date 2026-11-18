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
#include "hw/misc/esp_board_glue.h"
#include "sysemu/reset.h"
#include "qemu/main-loop.h"
#include "qemu/log.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "hw/irq.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qapi/error.h"
#include "qom/object.h"

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <dirent.h>

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
/* Where the advertising set's record sits in exchange memory. Read off a
 * running controller (ADR 0044), not from a document. */
#define ADV_RECORD_OFFSET 0x1400
/* The receive descriptor ring, and how the ROM walks it. All read off the
 * ROM's own code (ADR 0046), none of it from a document. */
#define RX_RING_OFFSET    0x1000
#define RX_RING_COUNT     10
#define RX_DESC_SIZE      20
#define RX_DONE           0x8000     /* +0: a packet is here */
#define RX_ERROR_MASK     0x403D     /* +2: clear means it is good */
#define PDU_ADV_IND       0x00       /* +4, low four bits */
#define PDU_CONNECT_IND   0x05
/* The handler reads a halfword here and treats it as the exchange-memory
 * offset of the packet's own bytes, then copies this many of them
 * (r_lld_adv_pkt_rx_connect_ind). Thirty-four is a connection request:
 * six bytes of initiator address, six of advertiser, and twenty-two of
 * connection data. */
#define RX_DESC_PAYLOAD   18
#define CONNECT_IND_SIZE  34
/* The most any legacy packet carries: thirty-seven bytes of advertising
 * payload, of which a connection request is thirty-four, and twenty-seven on
 * a connection. Capping the copy here is what keeps a malformed frame off the
 * air from writing past the buffer the descriptor named. */
#define MAX_PDU_BYTES     37
/* When the packet arrived, which the hardware records and the link layer
 * needs: r_lld_adv_pkt_rx_connect_post builds a connection's anchor point out
 * of these. The clock is the same twenty-eight bit half-slot count the
 * exchange table's entries are timed in, split low half at +8 and high at
 * +10 -- the ROM raises an internal error if anything above those twenty-eight
 * bits is set. Within the half-slot, +12 is a position the ROM reads as
 * `624 - fine`, so it is in the same half-microseconds as everything else
 * here. The top two bits of +6 choose the rate.
 *
 * The time recorded is the event's own start, not the moment the model
 * happens to hand the packet over. A connection resynchronises itself to
 * this timestamp every event, so handing it the clock at kick time -- some
 * two milliseconds before the event opens -- drags the anchor earlier and
 * the link runs a millisecond fast. */
#define RX_DESC_RATE      6          /* in bits 15..14 */
#define RX_DESC_CLOCK_LO  8
#define RX_DESC_CLOCK_HI  10
#define RX_DESC_FINE      12
#define RX_CLOCK_HI_MASK  0x0FFF     /* the clock is twenty-eight bits */
/* +12 carries two things. Its low ten bits are the position within the
 * half-slot -- r_lld_adv_pkt_rx_connect_post reads exactly ten of them -- and
 * bits 15..11 name the activity the packet belongs to. r_lld_rxdesc_check
 * compares those against the link it was asked about and reports nothing
 * received when they differ, so a descriptor without them is invisible to a
 * connection however complete it otherwise looks. Advertising is activity
 * zero, which is why it never needed this. */
#define RX_DESC_FINE_MASK   0x03FF
#define RX_DESC_LINK_SHIFT  11
/* And which activity an event belongs to comes off the entry itself: the
 * control structure it names sits at link * 45 + 512 halfwords. */
#define CS_BASE_HALFWORDS     512
#define CS_HALFWORDS_PER_LINK 45
/* Where an event's control structure is, and what the hardware says about how
 * the event went.
 *
 * r_sch_prog_ble_push (ROM 0x4002d674) writes the structure's exchange-memory
 * address into the entry it is filling, at +8, counted in halfwords:
 * link * 45 + 512, which is (link * 90 + 1024) / 2. So an entry names its own
 * structure, and this model does not have to know which link an event belongs
 * to -- it reads it off the entry, as the baseband would.
 *
 * r_lld_con_frm_isr then reads the high byte of the halfword at +86 of that
 * structure and will not treat the event as having gone cleanly unless it is
 * zero. Nothing in the ROM ever writes that byte: it is the hardware's to
 * write, so left alone it is whatever the memory happened to hold, and a
 * connection is told every one of its events failed.
 */
#define ET_CS_HALFWORD    8        /* the entry's +8, the structure in halfwords */
#define CS_EVENT_STATUS   87       /* the high byte of the halfword at +86 */
/* Where the link layer keeps the index of the descriptor it reads next.
 * A ROM data address, stable for this ROM, and the only way to know
 * which of the ten the ROM will look at. */
#define LLD_ENV_PTR       0x3FCEFF78
#define LLD_ENV_RX_INDEX  216
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
/* A packet arrived during the event. r_rwble_isr (ROM 0x4002b564) hands this
 * bit to r_sch_prog_rx_isr, which calls the event's own callback with a reason
 * of 2 -- and that is the only way a connection ever looks at what it
 * received. Advertising does not need it: its end callback walks the
 * descriptor ring itself. The bit is raised with the end, not before it,
 * because r_rwble_isr takes them in that order out of one status word and the
 * receive handler insists the entry's status is already past 2. */
#define BIT_RX          (1u << 2)

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
    char *air;                  /* directory shared with other radios */
    char *air_name;             /* what this one is called in it */
    int air_fd;
    uint64_t sent;
    uint64_t received;

    uint32_t regs[REG_COUNT];   /* what the guest wrote, word by word */
    uint32_t raw;               /* pending status bits */
    uint32_t slot, fine;        /* the last clock sample */
    uint32_t fifo[FIFO_DEPTH];
    unsigned fifo_len;

    /* Exchange-table entries kicked and not yet ended, by start time. */
    struct { unsigned idx; int64_t start_hus; bool received; bool late; } events[ET_MAX_PENDING];
    unsigned n_events;
    QEMUTimer *event_timer;

    QEMUTimer *timer;
    qemu_irq irq_line;
    int irq_level;
    QEMUTimer *irq_timer;          /* the deferred interrupt (ESP_IRQ_DEFER_NS) */
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

static void ble_irq_due(void *opaque)
{
    EspBleCore *s = opaque;
    int level = ((s->raw & s->regs[R_INTCNTL / 4]) || s->fifo_len) ? 1 : 0;

    if (!s->irq_line || level == s->irq_level) {
        return;
    }
    s->irq_level = level;
    qemu_set_irq(s->irq_line, level);
}

/* After a machine reset, drive the level this device holds, whatever it last
 * drove. A peripheral reset clears the interrupt matrix's record of every
 * source, and this device is not reset with it (ADR 0035): forwarding only a
 * change would then lose the next interrupt, the matrix holding 0 and this
 * device 1. After a CPU reset the matrix kept the level, and it forwards only a
 * change, so driving the same level again does nothing (ADR 0142). */
static void ble_reset(void *opaque)
{
    EspBleCore *s = opaque;

    s->irq_level = -1;
    ble_irq_due(s);
}

/* Deferred for the same reason as the I2C master's: raising the line inside
 * the guest's own write would deliver the interrupt on the next instruction,
 * before the setter has finished. On the guest's clock (ADR 0113). */
static void ble_update_irq(EspBleCore *s)
{
    if (s->irq_timer && !timer_pending(s->irq_timer)) {
        timer_mod_ns(s->irq_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                   + ESP_IRQ_DEFER_NS);
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

/* Investigation hatch, off unless BOARDEMUL_BLE_PEEK is set.
 *
 * Resolve an exchange-memory offset the way the ROM does: the page is the
 * offset rounded down to a kilobyte, the register serving it is the one whose
 * top fourteen bits hold that page, and the address is its low bits scaled by
 * four, plus the offset within the page.
 */
static hwaddr ble_em_addr(EspBleCore *s, uint32_t offset)
{
    uint32_t page = offset & ~0x3FFu;

    for (unsigned i = 0; i < 64; i++) {
        uint32_t reg = s->regs[(R_EM_BASE0 / 4) + i];
        if ((((reg >> 18) & 0x3FFF) << 2) == page && (reg & 0x3FFFF) != 0) {
            return (0x3FC00000u | ((reg & 0x3FFFF) << 2)) + (offset - page);
        }
    }
    return 0;
}

/* Investigation hatch, off unless BOARDEMUL_BLE_SCHED is set.
 *
 * The end-of-event handler (r_sch_prog_end_isr, ROM 0x4002d58c) does not
 * dispatch on the entry itself: it takes the index from a byte at offset 256
 * of a table in RAM, reads a callback pointer sixteen bytes into that table
 * per index, and skips the call outright when the pointer is null. So whether
 * an event is ever serviced is decided in this table, not by the status the
 * entry ends with -- which is why it is worth reading.
 */
#define SCHED_TABLE   0x3FCEFA8Cu
#define SCHED_STRIDE  16
#define SCHED_CURRENT 256

static void ble_sched_peek(EspBleCore *s, unsigned idx, const char *when)
{
    static int enabled = -1;
    uint32_t slot[4] = {0};
    uint8_t current = 0;

    if (enabled < 0) {
        enabled = getenv("BOARDEMUL_BLE_SCHED") != NULL;
    }
    if (!enabled) {
        return;
    }
    address_space_read(&address_space_memory,
                       SCHED_TABLE + (hwaddr)idx * SCHED_STRIDE,
                       MEMTXATTRS_UNSPECIFIED, slot, sizeof(slot));
    address_space_read(&address_space_memory, SCHED_TABLE + SCHED_CURRENT,
                       MEMTXATTRS_UNSPECIFIED, &current, 1);
    qemu_log_mask(LOG_UNIMP, "esp-ble-core: sched %s entry %u: arg0 0x%08x "
                  "callback 0x%08x arg2 0x%08x word3 0x%08x, current %u%c",
                  when, idx, slot[0], slot[1], slot[2], slot[3], current, 10);
}

static void ble_peek(EspBleCore *s, unsigned idx)
{
    static int enabled = -1;
    static unsigned shown;
    uint8_t buf[32];
    hwaddr at;

    if (enabled < 0) {
        enabled = getenv("BOARDEMUL_BLE_PEEK") != NULL;
    }
    if (!enabled || shown >= 3) {
        return;
    }
    shown++;

    /* The advertising set's descriptor, and the two offsets it holds. */
    at = ble_em_addr(s, 0x1400);
    if (!at) {
        qemu_log_mask(LOG_UNIMP, "esp-ble-core: peek: offset 0x1400 unmapped%c", 10);
        return;
    }
    address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED, buf, sizeof(buf));
    qemu_log_mask(LOG_UNIMP, "esp-ble-core: peek entry %u: 0x1400 -> 0x%08llx: "
                  "%02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x%c",
                  idx, (unsigned long long)at,
                  buf[0], buf[1], buf[2], buf[3], buf[4], buf[5], buf[6], buf[7],
                  buf[8], buf[9], buf[10], buf[11], buf[12], buf[13], buf[14], buf[15], 10);
    qemu_log_mask(LOG_UNIMP, "esp-ble-core: peek entry %u rest: "
                  "%02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x %02x%02x%c",
                  idx,
                  buf[16], buf[17], buf[18], buf[19], buf[20], buf[21], buf[22], buf[23],
                  buf[24], buf[25], buf[26], buf[27], buf[28], buf[29], buf[30], buf[31], 10);

    /* Follow every halfword that could be an offset of its own, not just the
     * first two: a pointer to the payload could sit anywhere in the record. */
    for (int half = 0; half < 16; half++) {
        uint32_t ptr = buf[half * 2] | (buf[half * 2 + 1] << 8);
        if (ptr < 0x400 || ptr >= 0x8000) {
            continue;
        }
        hwaddr to = ble_em_addr(s, ptr);
        if (!to) {
            qemu_log_mask(LOG_UNIMP, "esp-ble-core: peek:   0x%04x unmapped%c", ptr, 10);
            continue;
        }
        uint8_t seen[16];
        address_space_read(&address_space_memory, to, MEMTXATTRS_UNSPECIFIED, seen, sizeof(seen));
        qemu_log_mask(LOG_UNIMP, "esp-ble-core: peek:   0x%04x -> 0x%08llx: "
                      "%02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x %02x%c",
                      ptr, (unsigned long long)to,
                      seen[0], seen[1], seen[2], seen[3], seen[4], seen[5],
                      seen[6], seen[7], seen[8], seen[9], seen[10], seen[11], 10);
    }
}

/* The air, as ADR 0036 defines it: a directory in which every radio leaves a
 * file naming the port it listens on, and transmitting means handing the frame
 * to everyone else listed. Bluetooth gets a directory of its own because it is
 * a different band -- a LoRa radio cannot hear these and should not (ADR 0045).
 *
 * It is spoken here in C rather than through the Python side, because the only
 * thing that knows a packet exists is this device: it holds the base registers
 * that say where the controller mapped it.
 */
static void ble_air_join(EspBleCore *s)
{
    struct sockaddr_in me = { .sin_family = AF_INET };
    socklen_t len = sizeof(me);
    char path[PATH_MAX], port[16];
    int fd;

    if (!s->air || !s->air[0]) {
        return;
    }
    fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) {
        warn_report("esp-ble-core: no socket for the air: %s", strerror(errno));
        return;
    }
    me.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    me.sin_port = 0;
    if (bind(fd, (struct sockaddr *)&me, sizeof(me)) < 0 ||
        getsockname(fd, (struct sockaddr *)&me, &len) < 0) {
        warn_report("esp-ble-core: could not join the air: %s", strerror(errno));
        close(fd);
        return;
    }
    s->air_fd = fd;

    snprintf(path, sizeof(path), "%s/%s.air", s->air,
             s->air_name && s->air_name[0] ? s->air_name : "ble");
    snprintf(port, sizeof(port), "%u", ntohs(me.sin_port));
    FILE *f = fopen(path, "w");
    if (f) {
        fputs(port, f);
        fclose(f);
        info_report("esp-ble-core: on the air as %s on port %s", path, port);
    }
}

static void ble_air_send(EspBleCore *s, const uint8_t *frame, size_t length)
{
    struct dirent *entry;
    DIR *dir;

    if (s->air_fd <= 0 || !length) {
        return;
    }
    dir = opendir(s->air);
    if (!dir) {
        return;
    }
    while ((entry = readdir(dir))) {
        char path[PATH_MAX], own[PATH_MAX];
        struct sockaddr_in to = { .sin_family = AF_INET };
        unsigned port = 0;
        FILE *f;

        const char *dot = strrchr(entry->d_name, '.');
        if (!dot || strcmp(dot, ".air")) {
            continue;
        }
        snprintf(own, sizeof(own), "%s.air",
                 s->air_name && s->air_name[0] ? s->air_name : "ble");
        if (!strcmp(entry->d_name, own)) {
            continue;                   /* a radio does not hear itself */
        }
        snprintf(path, sizeof(path), "%s/%s", s->air, entry->d_name);
        f = fopen(path, "r");
        if (!f) {
            continue;
        }
        if (fscanf(f, "%u", &port) == 1 && port) {
            to.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            to.sin_port = htons((uint16_t)port);
            if (sendto(s->air_fd, frame, length, 0,
                       (struct sockaddr *)&to, sizeof(to)) < 0) {
                /* A radio that has gone leaves its file behind for a moment. */
            }
        }
        fclose(f);
    }
    closedir(dir);
    s->sent++;
}

/* Collect what the controller staged for this event.
 *
 * The advertising record holds offsets, and the ones that matter point at
 * advertising data: a run of structures, each a length, a type, and that many
 * bytes. Walking them is how the length is known without a constant, and how
 * a run of zeroes is told from a payload.
 */
static size_t ble_collect_payload(EspBleCore *s, uint8_t *out, size_t room)
{
    uint8_t record[32];
    size_t filled = 0;
    hwaddr at = ble_em_addr(s, ADV_RECORD_OFFSET);

    if (!at) {
        return 0;
    }
    address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                       record, sizeof(record));

    uint32_t taken[16];
    unsigned n_taken = 0;

    for (int half = 0; half < 16 && filled < room; half++) {
        uint32_t offset = record[half * 2] | (record[half * 2 + 1] << 8);
        uint8_t block[32];
        hwaddr to;
        bool already = false;

        if (offset < 0x400 || offset >= 0x8000) {
            continue;
        }
        /* The record holds its own offset among the others; following that
         * puts the record itself on the air dressed as a payload. */
        if (offset == ADV_RECORD_OFFSET) {
            continue;
        }
        for (unsigned i = 0; i < n_taken; i++) {
            already |= taken[i] == offset;
        }
        if (already) {
            continue;                   /* the same block, referenced twice */
        }
        to = ble_em_addr(s, offset);
        if (!to) {
            continue;
        }
        address_space_read(&address_space_memory, to, MEMTXATTRS_UNSPECIFIED,
                           block, sizeof(block));

        /* Walk the structures. A length of zero ends the run, and anything
         * that does not fit is not advertising data at all. */
        size_t walked = 0;
        while (walked < sizeof(block)) {
            uint8_t length = block[walked];
            if (length == 0 || walked + 1 + length > sizeof(block)) {
                break;
            }
            walked += 1 + length;
        }
        if (walked == 0 || walked > room - filled) {
            continue;
        }
        memcpy(out + filled, block, walked);
        filled += walked;
        taken[n_taken++] = offset;
    }
    return filled;
}

static void ble_transmit(EspBleCore *s)
{
    uint8_t payload[64];
    size_t length;

    if (s->air_fd <= 0) {
        return;
    }
    length = ble_collect_payload(s, payload + 1, sizeof(payload) - 1);
    if (!length) {
        return;
    }
    /* A frame on this air is the packet type and then the payload, so a
     * receiver need not guess what it is holding (ADR 0046). */
    payload[0] = PDU_ADV_IND;
    ble_air_send(s, payload, length + 1);
}

/* Take whatever has arrived on the air. Called as an event completes, which
 * is when the controller would have been listening. */
static bool ble_air_poll(EspBleCore *s, uint8_t *frame, size_t room, size_t *got)
{
    ssize_t n;

    if (s->air_fd <= 0) {
        return false;
    }
    n = recv(s->air_fd, frame, room, MSG_DONTWAIT);
    if (n <= 0) {
        return false;
    }
    *got = (size_t)n;
    qemu_log_mask(LOG_UNIMP, "esp-ble-core: heard %d byte(s) on the air%c", (int)n, 10);
    return true;
}

/* Hand a received packet to the link layer the way its hardware would: fill
 * the descriptor it is about to read, and say what kind of packet it was.
 */
static bool ble_deliver(EspBleCore *s, const uint8_t *frame, size_t length,
                        unsigned link, int64_t at)
{
    uint32_t env = 0;
    uint8_t index = 0;
    hwaddr ring = ble_em_addr(s, RX_RING_OFFSET);
    hwaddr desc;
    uint16_t done = RX_DONE, status = 0, kind;

    if (!length) {
        return false;
    }
    /* The descriptor's +4 is the packet's own two header bytes, not just its
     * type: the first says what kind of packet it is -- an advertising type,
     * or on a connection the LLID and the sequence bits -- and the second is
     * how many bytes follow. r_lld_con_rx reads that length out of the high
     * byte and refuses a zero LLID, so a connection's packets cannot be read
     * at all from the type alone. */
    kind = (uint16_t)frame[0] | (uint16_t)((length - 1) << 8);
    if (!ring) {
        qemu_log_mask(LOG_UNIMP, "esp-ble-core: deliver: ring unmapped%c", 10);
        return false;
    }
    address_space_read(&address_space_memory, LLD_ENV_PTR, MEMTXATTRS_UNSPECIFIED,
                       &env, sizeof(env));
    if (env < 0x3FC00000u || env >= 0x3FD00000u) {
        qemu_log_mask(LOG_UNIMP, "esp-ble-core: deliver: env 0x%08x looks wrong%c",
                      env, 10);
        return false;                         /* the link layer is not up yet */
    }
    address_space_read(&address_space_memory, env + LLD_ENV_RX_INDEX,
                       MEMTXATTRS_UNSPECIFIED, &index, 1);
    if (index >= RX_RING_COUNT) {
        qemu_log_mask(LOG_UNIMP, "esp-ble-core: deliver: index %u out of range%c",
                      index, 10);
        return false;
    }
    desc = ring + (hwaddr)index * RX_DESC_SIZE;

    address_space_write(&address_space_memory, desc + 2, MEMTXATTRS_UNSPECIFIED,
                        &status, sizeof(status));
    address_space_write(&address_space_memory, desc + 4, MEMTXATTRS_UNSPECIFIED,
                        &kind, sizeof(kind));

    /* Say when it arrived. A descriptor left without this is not merely
     * missing a detail: a connection request's anchor point is computed from
     * it, so whatever the last user of this descriptor left behind becomes the
     * time the connection is supposed to start (ADR 0049). The rate is written
     * rather than left alone for the same reason. */
    uint16_t clock_lo = (uint16_t)((at / HUS_PER_HS) & 0xFFFF);
    uint16_t clock_hi = (uint16_t)(((at / HUS_PER_HS) >> 16) & RX_CLOCK_HI_MASK);
    uint16_t fine = (uint16_t)(((at % HUS_PER_HS) & RX_DESC_FINE_MASK)
                               | ((link & 0x1F) << RX_DESC_LINK_SHIFT));
    uint16_t rate = 0;                  /* one megabit, as advertising is */

    address_space_write(&address_space_memory, desc + RX_DESC_RATE,
                        MEMTXATTRS_UNSPECIFIED, &rate, sizeof(rate));
    address_space_write(&address_space_memory, desc + RX_DESC_CLOCK_LO,
                        MEMTXATTRS_UNSPECIFIED, &clock_lo, sizeof(clock_lo));
    address_space_write(&address_space_memory, desc + RX_DESC_CLOCK_HI,
                        MEMTXATTRS_UNSPECIFIED, &clock_hi, sizeof(clock_hi));
    address_space_write(&address_space_memory, desc + RX_DESC_FINE,
                        MEMTXATTRS_UNSPECIFIED, &fine, sizeof(fine));
    /* Put the packet where the handler will look for it: the descriptor
     * already names a buffer, and that is the controller's own choice of
     * where its received bytes live. */
    uint16_t where = 0;
    address_space_read(&address_space_memory, desc + RX_DESC_PAYLOAD,
                       MEMTXATTRS_UNSPECIFIED, &where, sizeof(where));
    hwaddr into = where ? ble_em_addr(s, where) : 0;
    if (into && length > 1) {
        size_t bytes = length - 1;
        if (bytes > MAX_PDU_BYTES) {
            bytes = MAX_PDU_BYTES;
        }
        address_space_write(&address_space_memory, into, MEMTXATTRS_UNSPECIFIED,
                            frame + 1, bytes);
    }

    address_space_write(&address_space_memory, desc + 0, MEMTXATTRS_UNSPECIFIED,
                        &done, sizeof(done));
    s->received++;
    qemu_log_mask(LOG_UNIMP, "esp-ble-core: delivered a type %u packet of "
                  "%u byte(s) into descriptor %u at 0x%08llx, payload offset "
                  "0x%04x -> 0x%08llx (link %u)%c", frame[0] & 0x0F,
                  (unsigned)length - 1, index,
                  (unsigned long long)desc, where, (unsigned long long)into,
                  link, 10);
    return true;
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
    bool late = s->events[0].late;
    bool took = s->events[0].received;
    hwaddr entry = ble_et_entry(s, idx);
    uint16_t word0 = 0;

    address_space_read(&address_space_memory, entry, MEMTXATTRS_UNSPECIFIED, &word0, 2);
    word0 = (word0 & ~ET_STATUS_MASK) | ((late ? ET_SKIPPED : ET_ENDED) << ET_STATUS_SHIFT);
    address_space_write(&address_space_memory, entry, MEMTXATTRS_UNSPECIFIED, &word0, 2);
    qemu_log_mask(LOG_UNIMP, "esp-ble-core: entry %u %s at %lld hus%c", idx,
                  late ? "skipped" : "ended", (long long)ble_now_hus(), 10);

    /* Say how the event went. Nothing in this model can go wrong on the air,
     * so the only honest answer is that nothing did -- but it has to be said,
     * because the byte is the hardware's and the memory it sits in is not
     * cleared by anyone else (ADR 0051). Only the byte the ROM was seen to
     * read is written; the rest of the structure is not this model's to
     * invent. */
    uint16_t cs = 0;
    address_space_read(&address_space_memory, entry + ET_CS_HALFWORD,
                       MEMTXATTRS_UNSPECIFIED, &cs, sizeof(cs));
    hwaddr control = cs ? ble_em_addr(s, (uint32_t)cs * 2) : 0;
    if (control) {
        uint8_t clean = 0;
        address_space_write(&address_space_memory, control + CS_EVENT_STATUS,
                            MEMTXATTRS_UNSPECIFIED, &clean, 1);
    }
    ble_sched_peek(s, idx, "done");

    memmove(&s->events[0], &s->events[1], (s->n_events - 1) * sizeof(s->events[0]));
    s->n_events--;
    /* An event that never ran received nothing, whatever is in the ring. */
    uint32_t raised = late ? BIT_SKIP : BIT_END;
    if (took && !late) {
        raised |= BIT_RX;
    }
    s->raw |= raised;
    ble_fifo_push(s, raised);
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

    ble_peek(s, idx);
    ble_sched_peek(s, idx, "kick");
    ble_transmit(s);
    /* The entry names its own control structure, and the structure's place in
     * exchange memory says which link the event is for. */
    uint16_t cs = (uint16_t)(e[ET_CS_HALFWORD] | (e[ET_CS_HALFWORD + 1] << 8));
    unsigned link = cs >= CS_BASE_HALFWORDS
        ? (cs - CS_BASE_HALFWORDS) / CS_HALFWORDS_PER_LINK : 0;
    bool received = false;
    {
        uint8_t heard[64];
        size_t got = 0;
        if (ble_air_poll(s, heard, sizeof(heard), &got)) {
            received = ble_deliver(s, heard, got, link, start);
        }
    }

    unsigned i = s->n_events;
    while (i > 0 && s->events[i - 1].start_hus > start) {
        s->events[i] = s->events[i - 1];
        i--;
    }
    s->events[i].idx = idx;
    s->events[i].start_hus = start;
    s->events[i].received = received;
    /* An entry already past when it is kicked is skipped (ADR 0025). That is
     * decided here, on the kick: deciding it when the end timer runs measured
     * the timer, not the guest, and under -icount's clock warp a loaded host
     * runs that timer late on its own, which made an event the guest had
     * programmed in time look skipped. The tolerance is the one the end-time
     * test amounted to while the timer was punctual: two event lengths. */
    s->events[i].late = ble_now_hus() > start + 2 * EVENT_HUS;
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
        s->irq_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ble_irq_due, s);
        qemu_register_reset(ble_reset, s);
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ble_expire, s);
    s->event_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ble_event_done, s);
    ble_air_join(s);

    memory_region_init_io(&s->iomem, OBJECT(s), &ble_ops, s, TYPE_ESP_BLE_CORE, PAGE_SIZE);
    memory_region_add_subregion_overlap(get_system_memory(), s->base, &s->iomem, s->priority);
}

static Property ble_props[] = {
    DEFINE_PROP_STRING("intc", EspBleCore, intc_type),
    DEFINE_PROP_UINT64("base", EspBleCore, base, 0x60031000),
    DEFINE_PROP_INT32("priority", EspBleCore, priority, 100),
    DEFINE_PROP_INT32("irq", EspBleCore, irq_source, 8),
    DEFINE_PROP_STRING("air", EspBleCore, air),
    DEFINE_PROP_STRING("airname", EspBleCore, air_name),
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
