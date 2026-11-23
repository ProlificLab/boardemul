/*
 * SPDX-FileCopyrightText: 2026 Prolific Lab
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
/*
 * esp_usb_otg.c -- the ESP32-S3's USB OTG controller in device mode, at
 * 0x60080000, and the cable to a host (ADR 0166).
 *
 * The controller is Synopsys' DWC2. Offsets and fields are those of
 * Espressif's soc/esp32s3/include/soc/usb_struct.h and usb_reg.h; what is
 * modelled is what TinyUSB 0.15.0's dcd_esp32sx.c (the driver Espressif's
 * Arduino build of TinyUSB compiles for the S2 and S3) programs and reads.
 * Where the driver leaves behaviour to the core, the rules are DWC2's as the
 * OTG_FS chapter of ST's RM0090 states them for the same core.
 *
 *   - One receive FIFO of status entries, each followed by its data, read
 *     through any FIFO window: status 2 OUT data, 3 OUT transfer completed,
 *     4 SETUP stage done, 6 SETUP data.
 *   - One transmit FIFO per IN endpoint, written through the endpoint's own
 *     window ("Device EP n FIFO"); a packet leaves once the FIFO holds one,
 *     counting DIEPTSIZ down, and the last completes the transfer.
 *   - An OUT packet is taken while its endpoint is enabled, not NAKing and
 *     its transfer not complete; the core clears EPENA before it sets SETUP
 *     phase done or transfer completed. A SETUP is always taken.
 *   - The interrupt bits are derived as DWC2 derives them: RXFLVL from the
 *     receive FIFO, TXFE while an IN FIFO is empty, DAINT from the endpoint
 *     interrupts and their masks (TXFE's is DIEPEMPMSK), IEPINT and OEPINT
 *     from DAINT and DAINTMSK, the line from GINTSTS and GINTMSK under
 *     GAHBCFG's global mask.
 *
 * The host is at the other end of a character device, which carries packets,
 * not registers: a cable. Every message is a six-octet head -- type, endpoint,
 * a sixteen-bit argument, a sixteen-bit length -- and its data.
 *
 *   controller to host                     host to controller
 *   0x01 attached (left soft-disconnect)   0x81 SETUP, eight octets
 *   0x02 detached                          0x82 OUT packet on endpoint n
 *   0x03 ready: reset and recovered
 *   0x04 IN packet on n; argument = its maximum packet size
 *   0x05 the OUT packet on n was taken     0x8F done: the reply to the last
 *                                               message is complete
 *
 * A register access never crosses it (ADR 0166 says why that matters); each
 * message to the host waits for the host's reply to it, up to its done, so
 * the reply lands at the message's guest instant (otg_flush). The host holds
 * one OUT packet per endpoint at a time; until the endpoint takes it, it is
 * NAKed, and held here.
 *
 * The bus's times are the host's, kept here on the guest's clock: 100 ms of
 * attach debounce, a 10 ms reset, 10 ms of recovery before the first SETUP
 * (USB 2.0 7.1.7.3, 7.1.7.5, 9.2.6.2).
 */

#include "qemu/osdep.h"
#include "qemu/bswap.h"
#include "qemu/error-report.h"
#include "qemu/timer.h"
#include "qemu/main-loop.h"
#include "qemu/log.h"
#include "hw/misc/esp_board_glue.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-properties-system.h"
#include "hw/qdev-core.h"
#include "hw/irq.h"
#include "chardev/char-fe.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qapi/error.h"
#include "qom/object.h"
#include "sysemu/reset.h"
#include "hw/core/cpu.h"
#include "exec/cpu-common.h"

#define TYPE_ESP_USB_OTG "esp-usb-otg"
OBJECT_DECLARE_SIMPLE_TYPE(EspUsbOtg, ESP_USB_OTG)

#define OTG_SIZE        0x20000
#define EP_COUNT        7           /* USB_IN_EP_NUM / USB_OUT_EP_NUM */
#define TX_BYTES        1024        /* an empty IN FIFO reports 0x100 words */
#define PKT_MAX         1024
#define RX_ENTRIES      32
#define STORE_WORDS     (0x1000 / 4)

/* Global registers. */
#define R_GOTGCTL       0x000
#define R_GOTGINT       0x004
#define R_GAHBCFG       0x008
#define R_GRSTCTL       0x010
#define R_GINTSTS       0x014
#define R_GINTMSK       0x018
#define R_GRXSTSR       0x01C
#define R_GRXSTSP       0x020
#define R_GSNPSID       0x040
#define R_DCFG          0x800
#define R_DCTL          0x804
#define R_DSTS          0x808
#define R_DIEPMSK       0x810
#define R_DOEPMSK       0x814
#define R_DAINT         0x818
#define R_DAINTMSK      0x81C
#define R_DIEPEMPMSK    0x834
#define R_IN_EP         0x900
#define R_OUT_EP        0xB00
#define EP_STRIDE       0x20
#define EP_CTL          0x00
#define EP_INT          0x08
#define EP_TSIZ         0x10
#define EP_TXFSTS       0x18
#define R_FIFO          0x1000
#define FIFO_STRIDE     0x1000

/* The S3's GSNPSID ("OT", core 4.00a), as its TRM gives it. */
#define SNPSID          0x4F54400Au

/* GINTSTS. */
#define CURMOD          (1u << 0)
#define MODEMIS         (1u << 1)
#define OTGINT          (1u << 2)
#define SOF             (1u << 3)
#define RXFLVL          (1u << 4)
#define NPTXFEMP        (1u << 5)
#define GINNAKEFF       (1u << 6)
#define GOUTNAKEFF      (1u << 7)
#define ERLYSUSP        (1u << 10)
#define USBSUSP         (1u << 11)
#define USBRST          (1u << 12)
#define ENUMDNE         (1u << 13)
#define IEPINT          (1u << 18)
#define OEPINT          (1u << 19)
#define RESETDET        (1u << 23)
#define PTXFEMP         (1u << 26)
#define WKUPINT         (1u << 31)
/* What a write of one clears; RXFLVL, IEPINT and OEPINT are derived, the
 * FIFO empties are status, and the NAK-effective bits follow DCTL. */
#define GINTSTS_W1C     (MODEMIS | OTGINT | SOF | ERLYSUSP | USBSUSP | USBRST \
                         | ENUMDNE | (1u << 14) | (1u << 15) | (1u << 20)     \
                         | (1u << 21) | (1u << 22) | RESETDET | (1u << 28)    \
                         | (1u << 29) | (1u << 30) | WKUPINT)

/* GRSTCTL. */
#define CSFTRST         (1u << 0)
#define RXFFLSH         (1u << 4)
#define TXFFLSH         (1u << 5)
#define AHBIDL          (1u << 31)

/* GOTGCTL: a device on a cable, B-session valid, connector ID B. */
#define BSESVLD         (1u << 19)
#define CONIDSTS        (1u << 16)

/* DCTL, DCFG, DSTS. */
#define SFTDISCON       (1u << 1)
#define SGNPINNAK       (1u << 7)
#define CGNPINNAK       (1u << 8)
#define SGOUTNAK        (1u << 9)
#define CGOUTNAK        (1u << 10)
#define ENUMSPD_FULL_48 (3u << 1)

/* DIEPCTL / DOEPCTL. */
#define EPENA           (1u << 31)
#define EPDIS           (1u << 30)
#define SETD1PID        (1u << 29)
#define SETD0PID        (1u << 28)
#define SNAK            (1u << 27)
#define CNAK            (1u << 26)
#define NAKSTS          (1u << 17)
#define CTL_WRITE_ONLY  (SETD1PID | SETD0PID | SNAK | CNAK | EPDIS)
#define CTL_TXFNUM(c)   (((c) >> 22) & 0xF)

/* DIEPINT / DOEPINT. */
#define XFERCOMPL       (1u << 0)
#define EPDISBLD        (1u << 1)
#define SETUP           (1u << 3)
#define INEPNAKEFF      (1u << 6)
#define TXFE            (1u << 7)
#define STUPPKTRCVD     (1u << 15)

/* Receive status. */
#define PKT_OUT_DATA    2u
#define PKT_OUT_DONE    3u
#define PKT_SETUP_DONE  4u
#define PKT_SETUP_DATA  6u
#define RX_STATUS(kind, len, ep) (((kind) << 17) | ((len) << 4) | (ep))

/* The cable. */
#define MSG_HEAD        6
#define TO_HOST_ATTACHED    0x01
#define TO_HOST_DETACHED    0x02
#define TO_HOST_READY       0x03
#define TO_HOST_IN          0x04
#define TO_HOST_TAKEN       0x05
#define FROM_HOST_SETUP     0x81
#define FROM_HOST_OUT       0x82
#define FROM_HOST_DONE      0x8F
/* Messages to the host waiting their turn (otg_flush). */
#define OUTBOX              8

/* The host's times (USB 2.0): TATTDB, TDRST, TRSTRCY. */
#define ATTACH_DEBOUNCE_NS  (100 * 1000000LL)
#define RESET_NS            (10 * 1000000LL)
#define RECOVERY_NS         (10 * 1000000LL)
/* How long a driver that acknowledges a bus event and writes GINTSTS no more
 * is given to finish serving it. */
#define SERVE_GRACE_NS      (50 * 1000000LL)

/* Cores whose reads can hold a register, and how long a hold lasts at most:
 * longer than a core's turn, even QEMU's own 100 ms (ADR 0161). */
#define HOLD_CPUS       4
#define HOLD_NONE       ((hwaddr)-1)
#define HOLD_NS         (250 * 1000000LL)

enum { BUS_DETACHED, BUS_DEBOUNCE, BUS_RESET, BUS_ENUMERATED, BUS_RECOVERY,
       BUS_READY };
/* How far the driver has got with the bus event raised last. */
enum { SERVED_NOT, SERVED_ACKED, SERVED_CLOSED };

typedef struct {
    uint32_t ctl, intr, tsiz;
} OtgEp;

/* One access or event, for the in-memory trace (otg_note). */
typedef struct {
    int64_t us;
    int8_t cpu;
    char kind;
    uint16_t off;
    uint32_t value;
} OtgNote;

#define NOTES 1024

typedef struct {
    uint32_t status;
    uint16_t len;
    uint8_t data[PKT_MAX];
} RxEntry;

/* One message on the cable, either way. */
typedef struct {
    uint8_t type, ep;
    uint16_t arg, len;
    uint8_t data[PKT_MAX];
} OtgMsg;

struct EspUsbOtg {
    DeviceState parent_obj;

    MemoryRegion iomem;
    CharBackend chr;
    uint64_t base;
    int32_t priority;
    int32_t irq_source;
    char *intc_type;

    qemu_irq irq_line;
    int irq_level;
    QEMUTimer *irq_timer;
    QEMUTimer *bus_timer;

    uint32_t store[STORE_WORDS];    /* what is written and read back only */
    uint32_t gintsts, gintmsk, gahbcfg, gotgint, gotgctl;
    uint32_t dcfg, dctl, dsts;
    uint32_t diepmsk, doepmsk, daintmsk, empmsk;
    OtgEp in[EP_COUNT], out[EP_COUNT];
    bool out_complete[EP_COUNT];

    uint8_t tx[EP_COUNT][TX_BYTES];
    unsigned tx_len[EP_COUNT];

    RxEntry rx[RX_ENTRIES];
    unsigned rx_head, rx_count;
    uint8_t popped[PKT_MAX];        /* the data of the entry last popped */
    unsigned popped_len, popped_pos;

    uint8_t held[EP_COUNT][PKT_MAX];
    int held_len[EP_COUNT];         /* -1: the host holds nothing for it */

    int bus;
    int served;
    int64_t acked_at, reset_until;
    bool connected;
    uint8_t inbuf[2 * (MSG_HEAD + PKT_MAX)];
    unsigned inbuf_len;
    OtgMsg outbox[OUTBOX];
    unsigned out_head, out_count;
    bool flushing;
    bool said_tx_full, said_rx_full, said_bad_message;
    bool trace;
    bool sync;              /* wait for the host's reply (otg_flush) */
    /* A core between reading a register and its next access (otg_hold). */
    hwaddr hold_off[HOLD_CPUS];
    int64_t hold_until[HOLD_CPUS];
    uint64_t deferred, holds_expired, raises;
    OtgNote notes[NOTES];
    unsigned note_pos;
};

/* The last NOTES accesses and events, kept in memory at no cost worth the
 * name and written to the log on `qom-set <device> dump-trace on`: how a
 * race between the cores is seen without the file trace's own delay, which
 * changes the interleaving it is meant to show (ADR 0166). Kinds: r w the
 * guest's reads and writes; d a deferred access (value: the holder); i the
 * line (value: its level); t a message to the host (off: type, value: len). */
static void otg_note(EspUsbOtg *s, char kind, hwaddr off, uint32_t value)
{
    OtgNote *n = &s->notes[s->note_pos++ % NOTES];

    n->us = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000;
    n->cpu = current_cpu ? current_cpu->cpu_index : -1;
    n->kind = kind;
    n->off = (uint16_t)off;
    n->value = value;
}

static void otg_dump_trace(Object *obj, bool value, Error **errp)
{
    EspUsbOtg *s = ESP_USB_OTG(obj);
    unsigned count = MIN(s->note_pos, NOTES);

    if (!value) {
        return;
    }
    for (unsigned i = s->note_pos - count; i != s->note_pos; i++) {
        OtgNote *n = &s->notes[i % NOTES];
        qemu_log("otg %" PRId64 " cpu%d %c %05x %08x%c", n->us, n->cpu,
                 n->kind, n->off, n->value, 10);
    }
    qemu_log("otg trace: %u note(s) of %u%c", count, s->note_pos, 10);
}

static bool otg_dump_trace_get(Object *obj, Error **errp)
{
    return false;
}

/* -- derivation ---------------------------------------------------------- */

static unsigned xfersize_mask(int n)
{
    return n == 0 ? 0x7F : 0x7FFFF;
}

static unsigned pktcnt_mask(int n, bool out)
{
    return n == 0 ? (out ? 0x1 : 0x3) : 0x3FF;
}

static unsigned ep_size(const OtgEp *ep, int n)
{
    return ep->tsiz & xfersize_mask(n);
}

static unsigned ep_packets(const OtgEp *ep, int n, bool out)
{
    return (ep->tsiz >> 19) & pktcnt_mask(n, out);
}

static void ep_set_counts(OtgEp *ep, int n, bool out, unsigned size,
                          unsigned packets)
{
    uint32_t sm = xfersize_mask(n), pm = pktcnt_mask(n, out);

    ep->tsiz = (ep->tsiz & ~(sm | (pm << 19))) | (size & sm)
               | ((packets & pm) << 19);
}

static unsigned mps_in(EspUsbOtg *s, int n)
{
    static const unsigned ep0[4] = { 64, 32, 16, 8 };

    if (n == 0) {
        return ep0[s->in[0].ctl & 3];
    }
    return s->in[n].ctl & 0x7FF;
}

/* DOEPCTL0's packet size is DIEPCTL0's. */
static unsigned mps_out(EspUsbOtg *s, int n)
{
    unsigned mps = n == 0 ? mps_in(s, 0) : (s->out[n].ctl & 0x7FF);

    return mps ? mps : 64;
}

static uint32_t diepint(EspUsbOtg *s, int n)
{
    return s->in[n].intr | (s->tx_len[n] ? 0 : TXFE);
}

static uint32_t daint(EspUsbOtg *s)
{
    uint32_t bits = 0;

    for (int n = 0; n < EP_COUNT; n++) {
        uint32_t mask = s->diepmsk | ((s->empmsk >> n) & 1 ? TXFE : 0);
        if (diepint(s, n) & mask) {
            bits |= 1u << n;
        }
        if (s->out[n].intr & s->doepmsk) {
            bits |= 1u << (16 + n);
        }
    }
    return bits;
}

static uint32_t gintsts(EspUsbOtg *s)
{
    uint32_t value = s->gintsts & ~(RXFLVL | IEPINT | OEPINT | CURMOD);
    uint32_t pending = daint(s) & s->daintmsk;

    if (s->rx_count) {
        value |= RXFLVL;
    }
    if (pending & 0xFFFF) {
        value |= IEPINT;
    }
    if (pending >> 16) {
        value |= OEPINT;
    }
    return value;
}

/* -- the line ------------------------------------------------------------ */

static int otg_level(EspUsbOtg *s)
{
    return (s->gahbcfg & 1) && (gintsts(s) & s->gintmsk);
}

static void otg_irq_due(void *opaque)
{
    EspUsbOtg *s = opaque;
    int level = otg_level(s);

    if (s->irq_line && level != s->irq_level) {
        s->irq_level = level;
        s->raises += level;
        otg_note(s, 'i', 0, level);
        qemu_set_irq(s->irq_line, level);
    }
}

/* A line that falls falls at once, so a handler that has served everything
 * is not entered again for nothing; one that rises is deferred as the other
 * devices' are (ESP_IRQ_DEFER_NS), so it is not delivered inside the write
 * that caused it. On the guest's clock (ADR 0113). */
static void otg_update(EspUsbOtg *s)
{
    if (!s->irq_line) {
        return;
    }
    if (!otg_level(s)) {
        timer_del(s->irq_timer);
        otg_irq_due(s);
    } else if (s->irq_level == 0 && !timer_pending(s->irq_timer)) {
        timer_mod_ns(s->irq_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                   + ESP_IRQ_DEFER_NS);
    }
}

/* -- the cable ----------------------------------------------------------- */

static void otg_from_host(EspUsbOtg *s, uint8_t type, int n,
                          const uint8_t *data, unsigned len);

/* The next whole message from the host: from what has already arrived, or,
 * if `wait`, read from the cable until one has. */
static bool otg_take(EspUsbOtg *s, bool wait, OtgMsg *m)
{
    for (;;) {
        unsigned need = MSG_HEAD;
        if (s->inbuf_len >= MSG_HEAD) {
            unsigned len = lduw_le_p(s->inbuf + 4);
            if (len > PKT_MAX) {
                /* Out of step with the host: nothing after this can be read. */
                otg_from_host(s, s->inbuf[0], 0, NULL, len);
                s->inbuf_len = 0;
                return false;
            }
            need = MSG_HEAD + len;
            if (s->inbuf_len >= need) {
                m->type = s->inbuf[0];
                m->ep = s->inbuf[1];
                m->arg = lduw_le_p(s->inbuf + 2);
                m->len = len;
                memcpy(m->data, s->inbuf + MSG_HEAD, len);
                memmove(s->inbuf, s->inbuf + need, s->inbuf_len - need);
                s->inbuf_len -= need;
                return true;
            }
        }
        if (!wait || !s->connected) {
            return false;
        }
        int want = need - s->inbuf_len;
        if (qemu_chr_fe_read_all(&s->chr, s->inbuf + s->inbuf_len, want) != want) {
            qemu_log_mask(LOG_UNIMP, "esp-usb-otg: the cable went quiet in the "
                          "middle of a message; the host is gone%c", 10);
            s->connected = false;
            s->inbuf_len = 0;
            return false;
        }
        s->inbuf_len += want;
    }
}

/* Send what is waiting, one message at a time, each followed by everything
 * the host sends in reply, up to its DONE.
 *
 * So the host's reaction to an event -- the SETUP after the bus is ready, the
 * status stage after the last IN packet, the next packet after one is taken
 * -- lands at the guest instant of the event, not whenever the main loop
 * next reads the cable. On a timed air every run must be the same run to the
 * microsecond (ADR 0115), and a host answering on its own time made two runs
 * of one air differ by 1.5 ms at their first packet. Only what a person
 * types arrives on the host's own time, as typed input does anywhere. A
 * reply can cause another message (a packet taken), which waits its turn in
 * the outbox rather than nesting.
 *
 * Only with `sync`, which a run under -icount sets. There the wait costs the
 * guest no time and determinism depends on it. Without -icount the guest's
 * clock is the host's: a core waiting inside a register access watches its
 * own time pass, a loaded host makes every wait longer, and Meshtastic, which
 * logs dozens of packets a second, stopped meeting its own deadlines in a
 * busy suite. There is no determinism to keep on that clock anyway. */
static void otg_flush(EspUsbOtg *s)
{
    OtgMsg reply;

    s->flushing = true;
    while (s->out_count) {
        OtgMsg *m = &s->outbox[s->out_head];
        uint8_t head[MSG_HEAD];
        head[0] = m->type;
        head[1] = m->ep;
        stw_le_p(head + 2, m->arg);
        stw_le_p(head + 4, m->len);
        if (s->connected) {
            qemu_chr_fe_write_all(&s->chr, head, sizeof(head));
            if (m->len) {
                qemu_chr_fe_write_all(&s->chr, m->data, m->len);
            }
        }
        s->out_head = (s->out_head + 1) % OUTBOX;
        s->out_count--;
        if (!s->sync) {
            continue;       /* the reply comes when it comes (otg_receive) */
        }
        while (otg_take(s, true, &reply) && reply.type != FROM_HOST_DONE) {
            otg_from_host(s, reply.type, reply.ep, reply.data, reply.len);
        }
    }
    s->flushing = false;
}

static void otg_tell(EspUsbOtg *s, uint8_t type, int ep, unsigned arg,
                     const uint8_t *data, unsigned len)
{
    otg_note(s, 't', type, len | (ep << 16));
    if (s->trace) {
        qemu_log("otg %" PRId64 " tell 0x%02x ep%d len %u%c",
                 qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000, type, ep, len, 10);
    }
    if (!s->connected) {
        return;
    }
    if (s->out_count == OUTBOX) {
        qemu_log_mask(LOG_UNIMP, "esp-usb-otg: the outbox is full; message "
                      "0x%02x for endpoint %d was dropped%c", type, ep, 10);
        return;
    }
    OtgMsg *m = &s->outbox[(s->out_head + s->out_count++) % OUTBOX];
    m->type = type;
    m->ep = (uint8_t)ep;
    m->arg = (uint16_t)arg;
    m->len = (uint16_t)MIN(len, PKT_MAX);
    if (m->len) {
        memcpy(m->data, data, m->len);
    }
    if (!s->flushing) {
        otg_flush(s);
    }
}

static bool rx_push(EspUsbOtg *s, uint32_t status, const uint8_t *data,
                    unsigned len)
{
    if (s->rx_count == RX_ENTRIES) {
        if (!s->said_rx_full) {
            s->said_rx_full = true;
            qemu_log_mask(LOG_UNIMP, "esp-usb-otg: the receive FIFO is full; "
                          "a packet was lost%c", 10);
        }
        return false;
    }
    RxEntry *e = &s->rx[(s->rx_head + s->rx_count) % RX_ENTRIES];
    e->status = status;
    e->len = len;
    if (len) {
        memcpy(e->data, data, len);
    }
    s->rx_count++;
    return true;
}

/* The host's OUT packet on endpoint n, if the endpoint will take it now. */
static void otg_offer_out(EspUsbOtg *s, int n)
{
    OtgEp *ep = &s->out[n];
    int len = s->held_len[n];

    if (len < 0 || !(ep->ctl & EPENA) || (ep->ctl & NAKSTS)
        || s->out_complete[n] || s->rx_count + 2 > RX_ENTRIES) {
        return;
    }
    unsigned size = ep_size(ep, n), packets = ep_packets(ep, n, true);
    rx_push(s, RX_STATUS(PKT_OUT_DATA, (unsigned)len, (unsigned)n),
            s->held[n], len);
    size = size > (unsigned)len ? size - len : 0;
    packets = packets ? packets - 1 : 0;
    ep_set_counts(ep, n, true, size, packets);
    /* A short packet, or the size or count run out, ends the transfer. */
    if ((unsigned)len < mps_out(s, n) || size == 0 || packets == 0) {
        s->out_complete[n] = true;
        rx_push(s, RX_STATUS(PKT_OUT_DONE, 0, (unsigned)n), NULL, 0);
    }
    s->held_len[n] = -1;
    otg_tell(s, TO_HOST_TAKEN, n, 0, NULL, 0);
}

/* Every packet IN endpoint n has ready goes to the host. */
static void otg_send_in(EspUsbOtg *s, int n)
{
    OtgEp *ep = &s->in[n];

    while (ep->ctl & EPENA) {
        unsigned packets = ep_packets(ep, n, false);
        if (packets == 0) {
            break;
        }
        unsigned size = ep_size(ep, n);
        unsigned mps = mps_in(s, n);
        /* Full speed's largest packet is 1023 octets; the field allows 2047. */
        unsigned pkt = MIN(MIN(size, mps ? mps : 64), PKT_MAX);
        unsigned need = (pkt + 3) & ~3u;
        if (s->tx_len[n] < need) {
            return;
        }
        uint8_t data[PKT_MAX];
        memcpy(data, s->tx[n], pkt);
        memmove(s->tx[n], s->tx[n] + need, s->tx_len[n] - need);
        s->tx_len[n] -= need;
        ep_set_counts(ep, n, false, size - pkt, packets - 1);
        if (packets == 1) {
            ep->ctl &= ~EPENA;
            ep->intr |= XFERCOMPL;
        }
        otg_tell(s, TO_HOST_IN, n, mps, data, pkt);
    }
}

static void otg_from_host(EspUsbOtg *s, uint8_t type, int n,
                          const uint8_t *data, unsigned len)
{
    if (n >= EP_COUNT) {
        goto bad;
    }
    switch (type) {
    case FROM_HOST_SETUP:
        if (len != 8 || n != 0) {
            goto bad;
        }
        /* A SETUP ends whatever control transfer came before it. */
        s->held_len[0] = -1;
        rx_push(s, RX_STATUS(PKT_SETUP_DATA, 8, 0), data, 8);
        rx_push(s, RX_STATUS(PKT_SETUP_DONE, 0, 0), NULL, 0);
        return;
    case FROM_HOST_OUT:
        if (len > PKT_MAX) {
            goto bad;
        }
        memcpy(s->held[n], data, len);
        s->held_len[n] = len;
        otg_offer_out(s, n);
        return;
    default:
        break;
    }
bad:
    if (!s->said_bad_message) {
        s->said_bad_message = true;
        qemu_log_mask(LOG_UNIMP, "esp-usb-otg: the host sent message 0x%02x "
                      "for endpoint %d with %u octet(s), which is not one this "
                      "cable carries%c", type, n, len, 10);
    }
}

/* -- the bus ------------------------------------------------------------- */

static void otg_forget_host(EspUsbOtg *s)
{
    for (int n = 0; n < EP_COUNT; n++) {
        s->held_len[n] = -1;
    }
}

/* Whether the driver has served the bus event raised last: acknowledged it by
 * clearing its bit, and closed the handler's pass that did, which is its next
 * write to GINTSTS. A driver that writes nothing more is given a grace.
 *
 * On silicon the host's 10 ms are a thousand times a handler's pass, and
 * these orders hold by themselves. Here the guest's clock is the host's
 * unless the run counts instructions, and a loaded host can stall a vCPU
 * past 10 ms: then ENUMDNE arrives inside the pass that answered USBRST, the
 * pass's closing `gintsts |= ...` clears it, endpoint 0 is never sized, and
 * TinyUSB divides by its size of zero at the first SETUP (ADR 0166). Waiting
 * for the driver as well as the clock removes only what silicon cannot do. */
static bool otg_served(EspUsbOtg *s, int64_t now)
{
    return s->served == SERVED_CLOSED
           || (s->served == SERVED_ACKED && now >= s->acked_at + SERVE_GRACE_NS);
}

/* Not served yet: look again when the grace runs out, or when the driver
 * next writes GINTSTS (otg_status_written). */
static void otg_wait_served(EspUsbOtg *s)
{
    if (s->served == SERVED_ACKED) {
        timer_mod_ns(s->bus_timer, s->acked_at + SERVE_GRACE_NS);
    }
}

static void otg_raise_bus_event(EspUsbOtg *s, uint32_t bit)
{
    s->gintsts |= bit;
    s->served = SERVED_NOT;
}

static void otg_bus_step(void *opaque)
{
    EspUsbOtg *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    switch (s->bus) {
    case BUS_DEBOUNCE:
        /* Reset signalling starts, and the core reports it as it begins. */
        s->bus = BUS_RESET;
        s->dsts &= ~(3u << 1);
        otg_raise_bus_event(s, USBRST);
        otg_forget_host(s);
        s->reset_until = now + RESET_NS;
        timer_mod_ns(s->bus_timer, s->reset_until);
        break;
    case BUS_RESET:
        if (now < s->reset_until) {
            timer_mod_ns(s->bus_timer, s->reset_until);
            break;
        }
        if (!otg_served(s, now)) {
            otg_wait_served(s);
            break;
        }
        /* It ends: the core has the speed, full speed on a 48 MHz PHY. */
        s->bus = BUS_ENUMERATED;
        s->dsts = (s->dsts & ~(3u << 1)) | ENUMSPD_FULL_48;
        otg_raise_bus_event(s, ENUMDNE);
        break;
    case BUS_ENUMERATED:
        if (!otg_served(s, now)) {
            otg_wait_served(s);
            break;
        }
        s->bus = BUS_RECOVERY;
        timer_mod_ns(s->bus_timer, now + RECOVERY_NS);
        break;
    case BUS_RECOVERY:
        s->bus = BUS_READY;
        otg_tell(s, TO_HOST_READY, 0, 0, NULL, 0);
        break;
    default:
        break;
    }
    otg_update(s);
}

/* A write to GINTSTS, for the bus event being served (otg_served). */
static void otg_status_written(EspUsbOtg *s, uint32_t before)
{
    uint32_t bit = s->bus == BUS_RESET ? USBRST
                   : s->bus == BUS_ENUMERATED ? ENUMDNE : 0;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!bit) {
        return;
    }
    if ((before & bit) && !(s->gintsts & bit)) {
        s->served = SERVED_ACKED;
        s->acked_at = now;
    } else if (s->served == SERVED_ACKED) {
        s->served = SERVED_CLOSED;
        timer_mod_ns(s->bus_timer, now);
    }
}

/* The device is on the bus when it has left soft-disconnect and a host is on
 * the cable; the host sees the pull-up and, after its debounce, resets it. */
static void otg_bus_check(EspUsbOtg *s)
{
    bool on = s->connected && !(s->dctl & SFTDISCON);

    if (on && s->bus == BUS_DETACHED) {
        s->bus = BUS_DEBOUNCE;
        otg_tell(s, TO_HOST_ATTACHED, 0, 0, NULL, 0);
        timer_mod_ns(s->bus_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                   + ATTACH_DEBOUNCE_NS);
    } else if (!on && s->bus != BUS_DETACHED) {
        s->bus = BUS_DETACHED;
        timer_del(s->bus_timer);
        otg_forget_host(s);
        otg_tell(s, TO_HOST_DETACHED, 0, 0, NULL, 0);
    }
}

/* -- the registers ------------------------------------------------------- */

static void otg_core_reset(EspUsbOtg *s)
{
    memset(s->store, 0, sizeof(s->store));
    s->gintsts = NPTXFEMP | PTXFEMP;
    s->gintmsk = s->gahbcfg = s->gotgint = s->gotgctl = 0;
    s->dctl = SFTDISCON;
    s->dcfg = s->dsts = 0;
    s->diepmsk = s->doepmsk = s->daintmsk = s->empmsk = 0;
    memset(s->in, 0, sizeof(s->in));
    memset(s->out, 0, sizeof(s->out));
    memset(s->out_complete, 0, sizeof(s->out_complete));
    memset(s->tx_len, 0, sizeof(s->tx_len));
    s->rx_head = s->rx_count = 0;
    s->popped_len = s->popped_pos = 0;
    otg_bus_check(s);
}

static void otg_popped(EspUsbOtg *s, uint32_t status)
{
    unsigned kind = (status >> 17) & 0xF, n = status & 0xF;

    if (n >= EP_COUNT) {
        return;
    }
    if (kind == PKT_SETUP_DONE) {
        s->out[0].ctl &= ~EPENA;
        s->out[0].intr |= SETUP | STUPPKTRCVD;
    } else if (kind == PKT_OUT_DONE) {
        s->out[n].ctl &= ~EPENA;
        s->out[n].intr |= XFERCOMPL;
    }
}

static uint32_t otg_read_word(EspUsbOtg *s, hwaddr off)
{
    if (off >= R_FIFO) {
        uint32_t word = 0;
        for (unsigned i = 0; i < 4 && s->popped_pos < s->popped_len; i++) {
            word |= (uint32_t)s->popped[s->popped_pos++] << (8 * i);
        }
        return word;
    }
    if (off >= R_IN_EP && off < R_IN_EP + EP_COUNT * EP_STRIDE) {
        int n = (off - R_IN_EP) / EP_STRIDE;
        switch ((off - R_IN_EP) % EP_STRIDE) {
        case EP_CTL:
            return s->in[n].ctl;
        case EP_INT:
            return diepint(s, n);
        case EP_TSIZ:
            return s->in[n].tsiz;
        case EP_TXFSTS:
            return (TX_BYTES - s->tx_len[n]) / 4;
        default:
            return 0;
        }
    }
    if (off >= R_OUT_EP && off < R_OUT_EP + EP_COUNT * EP_STRIDE) {
        int n = (off - R_OUT_EP) / EP_STRIDE;
        switch ((off - R_OUT_EP) % EP_STRIDE) {
        case EP_CTL:
            return s->out[n].ctl;
        case EP_INT:
            return s->out[n].intr;
        case EP_TSIZ:
            return s->out[n].tsiz;
        default:
            return 0;
        }
    }
    switch (off) {
    case R_GINTSTS:
        return gintsts(s);
    case R_GINTMSK:
        return s->gintmsk;
    case R_GAHBCFG:
        return s->gahbcfg;
    case R_GOTGCTL:
        return s->gotgctl | BSESVLD | CONIDSTS;
    case R_GOTGINT:
        return s->gotgint;
    case R_GRSTCTL:
        return AHBIDL;
    case R_GRXSTSR:
    case R_GRXSTSP: {
        if (!s->rx_count) {
            return 0;
        }
        RxEntry *e = &s->rx[s->rx_head];
        uint32_t status = e->status;
        if (off == R_GRXSTSP) {
            memcpy(s->popped, e->data, e->len);
            s->popped_len = e->len;
            s->popped_pos = 0;
            s->rx_head = (s->rx_head + 1) % RX_ENTRIES;
            s->rx_count--;
            otg_popped(s, status);
        }
        return status;
    }
    case R_GSNPSID:
        return SNPSID;
    case R_DCFG:
        return s->dcfg;
    case R_DCTL:
        return s->dctl;
    case R_DSTS:
        return s->dsts;
    case R_DIEPMSK:
        return s->diepmsk;
    case R_DOEPMSK:
        return s->doepmsk;
    case R_DAINT:
        return daint(s);
    case R_DAINTMSK:
        return s->daintmsk;
    case R_DIEPEMPMSK:
        return s->empmsk;
    default:
        return off < 0x1000 ? s->store[off / 4] : 0;
    }
}

/* Every access, with the core that made it, when the `trace` property is on:
 * how a driver's races with itself are seen (ADR 0166). Off by default. */
static void otg_trace(EspUsbOtg *s, char kind, hwaddr offset, uint32_t value)
{
    otg_note(s, kind, offset, value);
    if (s->trace) {
        qemu_log("otg %" PRId64 " cpu%d %c %05" HWADDR_PRIx " %08x%c",
                 qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000,
                 current_cpu ? current_cpu->cpu_index : -1, kind, offset,
                 value, 10);
    }
}

/* A register the driver reads and writes back, from either core: the plain
 * ones, not the FIFO, the receive status that pops, or what a write clears. */
static bool otg_holdable(hwaddr off)
{
    if (off >= R_FIFO) {
        return false;
    }
    if (off >= R_IN_EP && off < R_OUT_EP + EP_COUNT * EP_STRIDE) {
        unsigned reg = (off - R_IN_EP) % EP_STRIDE;
        return reg == EP_CTL || reg == EP_TSIZ;
    }
    switch (off) {
    case R_GINTSTS:
    case R_GOTGINT:
    case R_GRXSTSR:
    case R_GRXSTSP:
    case R_GRSTCTL:
    case R_GSNPSID:
    case R_DSTS:
    case R_DAINT:
        return false;
    default:
        return true;
    }
}

/* Read-modify-writes, kept whole across the cores (ADR 0166).
 *
 * TinyUSB changes DIEPEMPMSK from its interrupt handler on one core and from
 * its task on the other, each with a read and a write and no lock. On silicon
 * the handler's takes a few cycles and the task's comes microseconds later,
 * so they never interleave. Here the cores take turns on one thread, and
 * QEMU ends a translation block at every register access, so a core's turn
 * can end between its read and its write: the other core then runs a whole
 * turn inside that window, its own read-modify-write is overwritten, and the
 * endpoint waits for ever on an interrupt it was never given.
 *
 * So a core that has read one of the plain registers holds it until its next
 * access to this device; another core that touches that register meanwhile
 * yields its turn and does the access again, after the holder has written.
 * The holder runs at once, being next in the round robin. That keeps the
 * order silicon's timing gives and nothing else: a hold never outlives the
 * holder's next access, or HOLD_NS for a core that reads and goes away. */
static void otg_hold(EspUsbOtg *s, hwaddr off)
{
    CPUState *cpu = current_cpu;
    int c = cpu ? cpu->cpu_index : -1;
    int64_t now;

    if (c < 0 || c >= HOLD_CPUS) {
        return;
    }
    s->hold_off[c] = HOLD_NONE;
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    for (int d = 0; d < HOLD_CPUS; d++) {
        if (d == c || s->hold_off[d] != off) {
            continue;
        }
        if (now >= s->hold_until[d]) {
            s->hold_off[d] = HOLD_NONE;
            s->holds_expired++;
            continue;
        }
        otg_note(s, 'd', off, d);
        if (!s->deferred++) {
            qemu_log_mask(LOG_UNIMP, "esp-usb-otg: core %d waited for core %d's "
                          "read-modify-write of 0x%03" HWADDR_PRIx
                          " (ADR 0166)%c", c, d, off, 10);
        }
        /* The access is abandoned, not finished: memory.c's re-entrancy
         * guard, engaged for it, would otherwise refuse every later one. */
        DEVICE(s)->mem_reentrancy_guard.engaged_in_io = false;
        cpu->exception_index = EXCP_YIELD;
        cpu_loop_exit_restore(cpu, cpu->mem_io_pc);
    }
}

static void otg_held_by_read(EspUsbOtg *s, hwaddr off)
{
    int c = current_cpu ? current_cpu->cpu_index : -1;

    if (c >= 0 && c < HOLD_CPUS && otg_holdable(off)) {
        s->hold_off[c] = off;
        s->hold_until[c] = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + HOLD_NS;
    }
}

static uint64_t otg_read(void *opaque, hwaddr offset, unsigned size)
{
    EspUsbOtg *s = opaque;
    uint32_t value;

    otg_hold(s, offset & ~3ULL);
    value = otg_read_word(s, offset & ~3ULL);
    otg_held_by_read(s, offset & ~3ULL);

    otg_trace(s, 'r', offset, value);
    /* A pop can free room for a packet the host is holding. */
    for (int n = 0; n < EP_COUNT; n++) {
        otg_offer_out(s, n);
    }
    otg_update(s);
    return value;
}

static void otg_write_in(EspUsbOtg *s, int n, unsigned reg, uint32_t v)
{
    OtgEp *ep = &s->in[n];

    switch (reg) {
    case EP_CTL: {
        bool enabling = (v & EPENA) && !(ep->ctl & EPENA);
        ep->ctl = (ep->ctl & EPENA) | (v & ~CTL_WRITE_ONLY);
        if (v & SNAK) {
            ep->intr |= INEPNAKEFF;
        }
        if ((v & EPDIS) && (ep->ctl & EPENA)) {
            ep->ctl &= ~EPENA;
            ep->intr |= EPDISBLD;
        }
        if (enabling) {
            otg_send_in(s, n);
        }
        break;
    }
    case EP_INT:
        ep->intr &= ~v;
        break;
    case EP_TSIZ:
        ep->tsiz = v;
        break;
    default:
        break;
    }
}

static void otg_write_out(EspUsbOtg *s, int n, unsigned reg, uint32_t v)
{
    OtgEp *ep = &s->out[n];

    switch (reg) {
    case EP_CTL: {
        bool enabling = (v & EPENA) && !(ep->ctl & EPENA);
        ep->ctl = (ep->ctl & (EPENA | NAKSTS)) | (v & ~(CTL_WRITE_ONLY | NAKSTS));
        if (v & SNAK) {
            ep->ctl |= NAKSTS;
        }
        if (v & CNAK) {
            ep->ctl &= ~NAKSTS;
        }
        if ((v & EPDIS) && (ep->ctl & EPENA)) {
            ep->ctl &= ~EPENA;
            ep->intr |= EPDISBLD;
        }
        if (enabling) {
            s->out_complete[n] = false;
        }
        otg_offer_out(s, n);
        break;
    }
    case EP_INT:
        ep->intr &= ~v;
        break;
    case EP_TSIZ:
        ep->tsiz = v;
        break;
    default:
        break;
    }
}

static void otg_write_word(EspUsbOtg *s, hwaddr off, uint32_t v)
{
    if (off >= R_FIFO) {
        int n = (off - R_FIFO) / FIFO_STRIDE;
        if (n >= EP_COUNT) {
            return;
        }
        if (s->tx_len[n] + 4 > TX_BYTES) {
            if (!s->said_tx_full) {
                s->said_tx_full = true;
                qemu_log_mask(LOG_UNIMP, "esp-usb-otg: a word written to the "
                              "full FIFO of IN endpoint %d was lost%c", n, 10);
            }
            return;
        }
        stl_le_p(s->tx[n] + s->tx_len[n], v);
        s->tx_len[n] += 4;
        otg_send_in(s, n);
        return;
    }
    if (off >= R_IN_EP && off < R_IN_EP + EP_COUNT * EP_STRIDE) {
        otg_write_in(s, (off - R_IN_EP) / EP_STRIDE,
                     (off - R_IN_EP) % EP_STRIDE, v);
        return;
    }
    if (off >= R_OUT_EP && off < R_OUT_EP + EP_COUNT * EP_STRIDE) {
        otg_write_out(s, (off - R_OUT_EP) / EP_STRIDE,
                      (off - R_OUT_EP) % EP_STRIDE, v);
        return;
    }
    switch (off) {
    case R_GINTSTS: {
        uint32_t before = s->gintsts;
        s->gintsts &= ~(v & GINTSTS_W1C);
        otg_status_written(s, before);
        break;
    }
    case R_GINTMSK:
        s->gintmsk = v;
        break;
    case R_GAHBCFG:
        s->gahbcfg = v;
        break;
    case R_GOTGCTL:
        s->gotgctl = v;
        break;
    case R_GOTGINT:
        s->gotgint &= ~v;
        break;
    case R_GRSTCTL:
        if (v & CSFTRST) {
            otg_core_reset(s);
            break;
        }
        if (v & RXFFLSH) {
            s->rx_count = 0;
            s->popped_len = s->popped_pos = 0;
        }
        if (v & TXFFLSH) {
            unsigned which = (v >> 6) & 0x1F;
            for (int n = 0; n < EP_COUNT; n++) {
                unsigned fifo = n == 0 ? 0 : CTL_TXFNUM(s->in[n].ctl);
                if (which == 0x10 || which == fifo) {
                    s->tx_len[n] = 0;
                }
            }
        }
        break;
    case R_DCFG:
        s->dcfg = v;
        break;
    case R_DCTL:
        s->dctl = v & ~(SGOUTNAK | CGOUTNAK | SGNPINNAK | CGNPINNAK);
        if (v & SGOUTNAK) {
            s->gintsts |= GOUTNAKEFF;
        }
        if (v & CGOUTNAK) {
            s->gintsts &= ~GOUTNAKEFF;
        }
        if (v & SGNPINNAK) {
            s->gintsts |= GINNAKEFF;
        }
        if (v & CGNPINNAK) {
            s->gintsts &= ~GINNAKEFF;
        }
        otg_bus_check(s);
        break;
    case R_DIEPMSK:
        s->diepmsk = v;
        break;
    case R_DOEPMSK:
        s->doepmsk = v;
        break;
    case R_DAINTMSK:
        s->daintmsk = v;
        break;
    case R_DIEPEMPMSK:
        s->empmsk = v;
        break;
    default:
        if (off < 0x1000) {
            s->store[off / 4] = v;
        }
        break;
    }
}

static void otg_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    EspUsbOtg *s = opaque;

    otg_hold(s, offset & ~3ULL);
    otg_trace(s, 'w', offset, (uint32_t)value);
    otg_write_word(s, offset & ~3ULL, (uint32_t)value);
    otg_update(s);
}

static const MemoryRegionOps otg_ops = {
    .read = otg_read,
    .write = otg_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

/* -- the character device ------------------------------------------------ */

static int otg_can_receive(void *opaque)
{
    EspUsbOtg *s = opaque;

    return sizeof(s->inbuf) - s->inbuf_len;
}

static void otg_receive(void *opaque, const uint8_t *buf, int size)
{
    EspUsbOtg *s = opaque;
    OtgMsg m;

    size = MIN(size, (int)(sizeof(s->inbuf) - s->inbuf_len));
    memcpy(s->inbuf + s->inbuf_len, buf, size);
    s->inbuf_len += size;
    /* What the host sends on its own: what a person typed. A DONE here
     * answers nothing that is waiting, and is let go. */
    while (otg_take(s, false, &m)) {
        if (m.type != FROM_HOST_DONE) {
            otg_from_host(s, m.type, m.ep, m.data, m.len);
        }
    }
    otg_update(s);
}

static void otg_event(void *opaque, QEMUChrEvent event)
{
    EspUsbOtg *s = opaque;

    if (event == CHR_EVENT_OPENED) {
        s->connected = true;
    } else if (event == CHR_EVENT_CLOSED) {
        s->connected = false;
        s->inbuf_len = 0;
    } else {
        return;
    }
    otg_bus_check(s);
    otg_update(s);
}

/* -- the device ---------------------------------------------------------- */

static void otg_reset(void *opaque)
{
    EspUsbOtg *s = opaque;

    for (int c = 0; c < HOLD_CPUS; c++) {
        s->hold_off[c] = HOLD_NONE;
    }
    otg_forget_host(s);
    otg_core_reset(s);
    otg_update(s);
}

static void otg_realize(DeviceState *dev, Error **errp)
{
    EspUsbOtg *s = ESP_USB_OTG(dev);

    if (s->irq_source >= 0) {
        Object *intc = object_resolve_path_type("", s->intc_type, NULL);
        if (!intc) {
            error_setg(errp, "esp-usb-otg: no '%s' in the machine",
                       s->intc_type);
            return;
        }
        s->irq_line = qdev_get_gpio_in(DEVICE(intc), s->irq_source);
        s->irq_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, otg_irq_due, s);
    }
    s->bus_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, otg_bus_step, s);
    s->bus = BUS_DETACHED;
    otg_forget_host(s);
    if (qemu_chr_fe_backend_connected(&s->chr)) {
        qemu_chr_fe_set_handlers(&s->chr, otg_can_receive, otg_receive,
                                 otg_event, NULL, s, NULL, true);
    }

    memory_region_init_io(&s->iomem, OBJECT(s), &otg_ops, s,
                          TYPE_ESP_USB_OTG, OTG_SIZE);
    memory_region_add_subregion_overlap(get_system_memory(), s->base,
                                        &s->iomem, s->priority);
    /* A plain device is not in the tree the reset phases walk, so it asks to
     * be reset with the machine, as the bus bridge does (ADR 0035). */
    qemu_register_reset(otg_reset, s);
    otg_reset(s);
}

static Property otg_props[] = {
    DEFINE_PROP_CHR("chardev", EspUsbOtg, chr),
    DEFINE_PROP_STRING("intc", EspUsbOtg, intc_type),
    DEFINE_PROP_UINT64("base", EspUsbOtg, base, 0x60080000),
    DEFINE_PROP_INT32("priority", EspUsbOtg, priority, 100),
    DEFINE_PROP_INT32("irq", EspUsbOtg, irq_source, 38),
    DEFINE_PROP_BOOL("trace", EspUsbOtg, trace, false),
    DEFINE_PROP_BOOL("sync", EspUsbOtg, sync, false),
    /* Counters, for `qom-get` from the monitor; not settings. */
    DEFINE_PROP_UINT64("deferred", EspUsbOtg, deferred, 0),
    DEFINE_PROP_UINT64("holds-expired", EspUsbOtg, holds_expired, 0),
    DEFINE_PROP_UINT64("raises", EspUsbOtg, raises, 0),
    DEFINE_PROP_END_OF_LIST(),
};

static void otg_instance_init(Object *obj)
{
    EspUsbOtg *s = ESP_USB_OTG(obj);

    s->intc_type = g_strdup("misc.esp32s3.intmatrix");
    s->irq_level = 0;
    object_property_add_bool(obj, "dump-trace", otg_dump_trace_get,
                             otg_dump_trace);
}

static void otg_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = otg_realize;
    device_class_set_props(dc, otg_props);
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
}

static const TypeInfo otg_info = {
    .name = TYPE_ESP_USB_OTG,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspUsbOtg),
    .instance_init = otg_instance_init,
    .class_init = otg_class_init,
};

static void otg_register_types(void)
{
    type_register_static(&otg_info);
}

type_init(otg_register_types)
