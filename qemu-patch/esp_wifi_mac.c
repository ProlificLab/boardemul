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
#include <string.h>
#include "sysemu/reset.h"
#include "hw/core/cpu.h"

#define TYPE_ESP_WIFI_MAC "esp-wifi-mac"
OBJECT_DECLARE_SIMPLE_TYPE(EspWifiMac, ESP_WIFI_MAC)

#define PAGE_SIZE       0x1000
#define REG_COUNT       (PAGE_SIZE / 4)
/* Where the driver puts the receive ring, read off a run (ADR 0054). */
#define R_RX_RING       0x088
/* And the command beside it. Having taken a descriptor back, the driver
 * sets bit 0 here and waits for the hardware to clear it -- the same
 * self-clearing shape as the reset gate above. Watched, not assumed: with
 * the status answered, the receive path read +0x090 and +0xc64, then set
 * this and span on it. There is no radio here to keep it busy, so the
 * command is taken as soon as it is given. */
#define R_RX_COMMAND    0x084
#define RX_COMMAND_GO   0x00000001u
/* Where the hardware got to. With the command taken, the driver reads
 * this next, always straight after the memory base at +0xc64 -- which is
 * the shape of an address being put back together out of a base and a
 * position. It reads nothing, and asks again. So this is where the model
 * has to say which descriptor it filled. */
#define R_RX_LAST       0x090
/* How big the frame was. Not in the buffer: the receive path reaches it
 * through a little accessor that selects a field by number, and the
 * fields it selects are the low eleven bits here, bits 16..23 of the same
 * register, and the low four of +0xcb0.
 *
 * The low eleven bits are NOT a length, which is what a first reading of the
 * accessor suggested and what feeding it a length and reading it back again
 * appeared to confirm. Its caller (0x420cecc4) treats the value as a mask: it
 * takes the lowest set bit, passes that bit's index to two handlers, clears
 * it and goes round again. So this is a bitmask of sources with work waiting,
 * eleven of them, and one of them is a frame having arrived. Which one is not
 * established, so BOARDEMUL_WIFI_QUEUE sets what is raised. */
/* The header the hardware writes in front of a received packet. Forty-eight
 * bytes, read off the ROM: on one branch the receive path computes
 * `buffer + 48 + n` and reads the packet there, and before that it reads the
 * words at +0, +4, +8, +44 and the byte at +47 -- so the packet starts at
 * forty-eight and five header fields are consulted on the way. */
#define RX_HEADER       48
#define RX_HEADER_TAG   8
#define R_RX_QUEUES     0xca8
/* Whether the MAC is running at all. ic_mac_deinit (ROM, 0x4003c5fc) spins
 * while bits 13 and 14 are set and then ORs 0x00ff1000 in; ic_mac_init
 * (0x4003c5e0) ANDs with 0xff00efff, which clears exactly those. So the eight
 * bits at 16..23 and bit 12 set is the MAC held, and nothing is received then. */
#define R_MAC_HOLD      0xca0
#define MAC_HELD        0x00ff1000u
/* And whether it is listening. The driver keeps 0x45 in this register and
 * toggles bit 8 on top of it: off before it reconfigures the MAC, on after.
 * Read off the trace rather than the code -- the same flash routine
 * (0x420d4792) writes 0x45 and 0x155 by turns around every channel change.
 * A frame delivered before the bit is first set is the run that never heard
 * anything: it landed between the ring being handed over and the interrupt
 * being routed, and was never serviced. */
#define R_RX_ENABLE     0x0d8
#define RX_ENABLED      0x00000100u
#define RX_QUEUE_MASK   0x7FFu
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
/* The transmit slots. Each is two words -- parameters at `0xd04 - slot*8` and
 * control at `0xd08 - slot*8` -- and the array runs *downwards*, which is the
 * same shape the receive side's per-source registers have (ADR 0055).
 *
 * Read off the ROM and then watched, in this order. `lmacTxFrame`'s last two
 * instructions are a read-modify-write of the control word ORing in
 * 0xc0000000, and its address is built as `(0x0c0067a1 - slot) << 3`, which is
 * 0x60033d08 - slot*8 exactly. A trace of a whole boot then shows the sequence
 * once and only once:
 *
 *     +0xd04 <- 0x0000000a   lmacSetTxFrame    the slot's parameters
 *     +0xd08 <- 0x006b9528   mac_tx_set_plcp0  the PLCP header
 *     +0xd04 <- 0x1200000a   lmacTxFrame       rate and backoff merged in
 *     +0xd08 <- 0xc06b9528   lmacTxFrame       the top two bits: go
 *
 * and then nothing, ever again. Those two bits are what a real MAC clears when
 * the frame has gone; nothing cleared them here, so the queue stayed busy,
 * `lmacIsIdle` said so, and every later `ppTxProtoProc` abandoned its send
 * without even reaching `lmacTxFrame`. That is the whole of why the station
 * could never associate.
 *
 * Only slot zero has ever been seen in use, so only slot zero is modelled.
 * The array plainly continues downwards -- eight would fit before running into
 * a register this file already names -- but every extra slot is a register
 * this model would start reinterpreting on the strength of a pattern rather
 * than an observation, and a write that happened to set both top bits would
 * then fake a transmission that never happened. One slot is what was
 * measured. */
#define R_TX_SLOT0      0xd08
/* One. The array does run downwards in eight-byte steps, but honouring five
 * of them cut the frames the station sent from dozens to one: the offsets
 * below the first are not all slots, and treating a write to one of them as
 * an arming clears bits in a register the driver is relying on. Measured,
 * not assumed -- the wider guess was tried and made it worse. */
#define TX_SLOTS        1
#define TX_ARMED        0xC0000000u
/* Which slots have finished. The driver's transmit-complete handler asks for
 * exactly one thing after acknowledging the interrupt: it calls the little
 * field accessor at 0x420d4fe8 with selector 2, which reads the low four bits
 * of this register. Watched, not guessed -- with the interrupt raised and this
 * left at zero the handler read it, found nothing, and never ended the frame
 * exchange, so the queue stayed busy exactly as before.
 *
 * Four bits beside an eleven-bit mask of receive sources at +0xca8 reads as
 * the same thing for transmit: a mask of slots with a completion waiting. */
#define R_TX_DONE_SLOTS 0xcb0
#define TX_DONE_MASK    0xFu
/* The ROM's LMAC transmit queue table: forty-byte entries indexed by slot,
 * from the literal `lmacIsIdle` and `lmacTxFrame` both load (0x4003b030).
 * `lmacTxFrame` stores the buffer it was given into word 0 of the entry, so
 * that is where the frame being transmitted can be found.
 *
 * Reading it is the same shortcut as reading `wDevCtrl+44` for the channel,
 * and called out for the same reason: a real MAC fetches the frame by DMA
 * from wherever the driver pointed it, and this is where the driver pointed
 * it -- but a device reaching into driver memory is a device relying on
 * something no wire carries. */
#define LMAC_QUEUES       0x3FCEF308
#define LMAC_QUEUE_STRIDE 40
/* Inside the buffer: the frame's own bytes, and its header and body lengths
 * as halfwords. Dumped rather than assumed -- at the store in `lmacTxFrame`
 * the buffer reads `+16: 88 95 cb 3f  18 00 23 00`, and following that pointer
 * lands on `40 00 ... 00 0d "xinyuandianzi"`: a probe request for the network
 * the firmware is looking for, twenty-four bytes of header and thirty-five of
 * body. Only the first octet and the sender's address are needed to answer
 * one, so nothing here depends on the lengths being right. */
#define EB_FRAME          16
#define EB_HEAD_LEN       20
#define EB_BODY_LEN       22
/* The most of a frame this device ever reads or writes. */
#define FRAME_MAX         BEACON_MAX
/* And how the driver says it has dealt with them. Found the same way, one
 * answer at a time: with the mask left at zero the handler read it and
 * stopped; with the completed slot in it, the handler read the mask, then read
 * this register -- which nothing had ever touched before -- and wrote the same
 * slot back. That is the ordinary split of a pending set and its
 * acknowledgement, the same shape as the interrupt status at +0xc3c and its
 * acknowledgement at +0xc40 a few registers along. */
#define R_TX_DONE_ACK   0xcac
/* Why the handler was woken. Found by raising the interrupt with nothing to
 * say and watching what it went and read: it reads this, gets nothing, and
 * returns -- then comes straight back, because the line is still asserted.
 * So a frame is not announced by the interrupt alone; it is announced here,
 * and the handler puts the line down by acknowledging what it has seen.
 *
 * Which bit means "a frame arrived" is read off the driver's own handler, not
 * guessed. The handler (0x4037a9a0, in IRAM) reads this register, ands it with
 * an enable mask, writes what it read back to +0xc40, and then dispatches on
 * individual bits to ROM routines whose addresses sit in its literal pool:
 *
 *     bits 14 and 24  ->  lmacProcessRxSucData    a frame arrived
 *     bits 15 and 21  ->  pp_post
 *     bit 7           ->  lmacPostTxComplete
 *     bit 19          ->  lmacProcessAllTxTimeout
 *     bit 8           ->  lmacProcessCollisions
 *
 * So raising all thirty-two bits, which this device did first, tells the driver
 * that a frame arrived AND that a transmission completed AND that everything
 * timed out AND that the medium collided. It runs every one of those handlers
 * on state that never happened, and that is the "different path" ADR 0055 saw
 * it take. One bit is the whole of what a received frame should say.
 *
 * BOARDEMUL_WIFI_STATUS still sets what is raised, because bit 24 is the other
 * candidate and nothing yet says which of the two a real MAC uses.
 */
#define R_INT_STATUS    0xc3c
/* And how it is put down. The handler does not write the status back --
 * it writes the bits it has dealt with here, which is the ordinary
 * split of a status register and its acknowledgement. Watched, not
 * assumed: with the status raised it read +0xc3c and wrote +0xc40. */
#define R_INT_CLEAR     0xc40
/* Bit 14: the one the driver's handler routes to lmacProcessRxSucData. */
#define RX_DONE_STATUS  0x00004000u
/* And bit 7, which the same handler routes to lmacPostTxComplete. */
#define TX_DONE_STATUS  0x00000080u

/* wDevCtrl_ptr, named by the ROM's linker script, and the byte in the
 * structure it points at that holds the channel the radio is tuned to. The
 * ROM's receive path reads the same byte to stamp the header. */
#define WDEV_CTRL_PTR   0x3FCEF93C
#define WDEV_CHANNEL    44

/* The part's ordinary DMA descriptor, as esp_i2s_master.c documents it. */
#define DESC_SIZE_MASK    0xFFFu
#define DESC_LENGTH_SHIFT 12
#define DESC_OWNER        (1u << 31)
#define DESC_EOF          (1u << 30)
#define DESC_BYTES        12

/* How far to follow a chain before deciding it is not one. */
#define DESC_MAX          16

/* A beacon, built rather than stored, because an access point's name is a
 * variable-length element in the middle of it and everything after moves.
 * The fields are the ones a scanning station reads: who it is from, what it
 * is called, what rates it offers, and which channel it is on -- the last of
 * which the driver checks against the channel the frame arrived on.
 *
 * The address is locally administered (bit 1 of the first byte), because a
 * model must not put a real vendor's address on the air.
 */
#define BEACON_MAX      128
#define SSID_MAX        32

/* The access point's own address, and the one every frame from it carries.
 * Locally administered, so no real vendor's address goes on the air. */
static const uint8_t ap_address[6] = { 0x02, 0x00, 0x00, 0x00, 0x00, 0x01 };
static const uint8_t everyone[6] = { 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF };

/* A management frame's twenty-four byte header. `kind` is the whole first
 * octet: the subtype in its top nibble and the type in the two bits below,
 * which for management frames are zero -- 0x80 a beacon, 0x50 a probe
 * response, 0xb0 an authentication, 0x10 an association response. */
static size_t build_header(uint8_t *out, uint8_t kind, const uint8_t *to,
                           uint16_t sequence)
{
    out[0] = kind;
    out[1] = 0x00;
    out[2] = 0x00;                                      /* duration */
    out[3] = 0x00;
    memcpy(out + 4, to, 6);                             /* who it is for */
    memcpy(out + 10, ap_address, 6);                    /* from us */
    memcpy(out + 16, ap_address, 6);                    /* our network */
    out[22] = (uint8_t)(sequence << 4);
    out[23] = (uint8_t)(sequence >> 4);
    return 24;
}

/* What a beacon and a probe response say, which is the same thing: the fixed
 * fields and then the elements a scanning station reads. */
static size_t build_body(uint8_t *out, const char *ssid, unsigned channel,
                         bool wpa2)
{
    size_t len = strlen(ssid);
    size_t at = 0;

    if (len > SSID_MAX) {
        len = SSID_MAX;
    }
    memset(out, 0, 8);                                  /* timestamp */
    at = 8;
    out[at++] = 0x64;                                   /* every 100 */
    out[at++] = 0x00;
    /* An infrastructure network, and one with a passphrase says so here as
     * well as in its RSN element: a station given one filters the open
     * networks out of its connect-time scan, so without the privacy bit the
     * firmware's own network is "not found" even while every scan lists it. */
    out[at++] = wpa2 ? 0x11 : 0x01;
    out[at++] = 0x00;
    out[at++] = 0x00;                                   /* the name */
    out[at++] = (uint8_t)len;
    memcpy(out + at, ssid, len);
    at += len;
    out[at++] = 0x01;                                   /* the rates it offers */
    out[at++] = 0x04;
    out[at++] = 0x82;
    out[at++] = 0x84;
    out[at++] = 0x8B;
    out[at++] = 0x96;
    out[at++] = 0x03;                                   /* and its channel */
    out[at++] = 0x01;
    out[at++] = (uint8_t)(channel ? channel : 1);
    if (wpa2) {
        static const uint8_t rsn[] = {
            0x30, 0x14,                                 /* RSN, twenty bytes */
            0x01, 0x00,                                 /* version 1 */
            0x00, 0x0F, 0xAC, 0x04,                     /* group: CCMP */
            0x01, 0x00, 0x00, 0x0F, 0xAC, 0x04,         /* one pairwise: CCMP */
            0x01, 0x00, 0x00, 0x0F, 0xAC, 0x02,         /* one key kind: PSK */
            0x00, 0x00,                                 /* no capabilities */
        };
        memcpy(out + at, rsn, sizeof(rsn));
        at += sizeof(rsn);
    }
    return at;
}

static size_t build_beacon(uint8_t *out, const char *ssid, unsigned channel,
                           bool wpa2)
{
    size_t at = build_header(out, 0x80, everyone, 0);
    return at + build_body(out + at, ssid, channel, wpa2);
}

/* Every frame on the air ends with a check sequence over its own bytes, and a
 * receiver that validates one will throw away a frame that has none. The
 * polynomial is the ordinary reflected CRC-32; it is written out here rather
 * than pulled in, because it is nine lines and this file has no other
 * dependency. */
static uint32_t frame_check(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xFFFFFFFFu;

    for (size_t i = 0; i < length; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc >> 1) ^ (0xEDB88320u & -(crc & 1));
        }
    }
    return ~crc;
}

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
    /* Armed when a frame is delivered: the reads that follow are the
     * handler asking why it was woken, and the register it consults is
     * the one this model has to answer. */
    unsigned trace_left;
    /* How many accesses to log after the first frame. BOARDEMUL_WIFI_TRACE. */
    unsigned trace_window;
    bool trace_from_start;
    /* Words of the receive header the run has been told to override, and
     * which of them. BOARDEMUL_WIFI_HEADER, as offset:value pairs. */
    uint32_t header_words[RX_HEADER / 4];
    uint32_t header_set;
    /* The transmit slots armed and not yet completed, and the timer that
     * completes them. A real MAC takes a frame's air time to send it. */
    QEMUTimer *tx_timer;
    uint32_t tx_pending;
    unsigned transmits;
    int64_t tx_delay_ns;
    /* A frame to put on next, when the station has sent something that wants
     * answering. It goes out through the same path as a beacon, so it waits
     * for the same conditions: a free descriptor and a MAC that is listening. */
    uint8_t pending[FRAME_MAX];
    size_t pending_len;
    uint16_t sequence;
    unsigned answers;
    /* One bit per management subtype the station has sent. A fixed cap on
     * the transmit log read as "the station only ever sent four frames",
     * which was a cap and not a measurement, and it sent the reasoning
     * that followed it wrong. Every new kind of frame is reported. */
    uint16_t kinds;
    /* The channel a delivered beacon claims. Zero means "whatever the
     * controller is tuned to". BOARDEMUL_WIFI_CHANNEL, or the property. */
    uint32_t channel;
    /* The access point this device puts in the air, if any. */
    char *ssid;
    uint32_t beacon_ms;
    bool wpa2;
    bool gated;
    uint32_t status_mask;
    uint32_t queue_mask;
    uint32_t header_tag;
    uint32_t ring;              /* what the driver handed us */
    hwaddr delivered_to;        /* the descriptor a frame went into */
    unsigned deliveries;
    unsigned taken;
    unsigned repeat;
    int64_t gap_ns;
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

/* The channel the controller is tuned to, or the one a run pinned. */
static unsigned wifi_channel(EspWifiMac *s)
{
    if (s->channel) {
        return s->channel;
    }
    uint32_t control = 0;
    uint8_t current = 0;
    address_space_read(&address_space_memory, WDEV_CTRL_PTR,
                       MEMTXATTRS_UNSPECIFIED, &control, sizeof(control));
    if (control) {
        address_space_read(&address_space_memory, control + WDEV_CHANNEL,
                           MEMTXATTRS_UNSPECIFIED, &current, sizeof(current));
    }
    unsigned channel = current & 0x0F;
    return (channel >= 1 && channel <= 14) ? channel : 1;
}

static void wifi_deliver(void *opaque)
{
    EspWifiMac *s = opaque;
    uint32_t word = 0;
    hwaddr desc;

    /* One frame at an arbitrary moment is a weak test of anything: a driver
     * only collects beacons while a scan is listening, and it hops channels
     * several times a second. So this keeps putting them on until told to
     * stop, and counts how many were taken. */
    if (s->delivered_to) {
        uint32_t previous[3] = { 0 };
        if (address_space_read(&address_space_memory, s->delivered_to,
                               MEMTXATTRS_UNSPECIFIED, previous,
                               sizeof(previous)) == MEMTX_OK
            && (previous[0] & DESC_OWNER)) {
            s->taken++;
        }
        s->delivered_to = 0;
    }
    if (s->repeat && s->deliveries >= s->repeat) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: put %u frame(s) on; the driver "
                      "took %u%c", s->deliveries, s->taken, 10);
        wifi_raise(s, 0);
        return;
    }
    if (!s->ring) {
        /* The driver hands the ring over while it starts its station, which is
         * well after this device is created. Said once, then waited for. */
        if (!s->deliveries) {
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: no receive ring handed "
                          "over yet; waiting%c", 10);
        }
        timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->gap_ns);
        return;
    }
    /* Nothing is heard while the MAC is held. ic_mac_deinit waits for the
     * two busy bits to drop and then sets these eight-plus-one bits;
     * ic_mac_init clears them, and the driver brackets every channel change
     * with the pair -- both read off the ROM, literals and all. A frame
     * delivered while held was the run that never heard anything: it landed
     * during bring-up, its interrupt was acknowledged unread, and with all
     * four descriptors filled and none ever re-armed the ring was dead. */
    if ((s->regs[R_MAC_HOLD / 4] & MAC_HELD)
        || !(s->regs[R_RX_ENABLE / 4] & RX_ENABLED)) {
        timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->gap_ns);
        return;
    }
    desc = wifi_free_descriptor(s, &word);
    if (!desc) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the ring at 0x%08x has no "
                      "descriptor the hardware owns after %u frame(s); waiting%c",
                      s->ring, s->deliveries, 10);
        timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->gap_ns);
        return;
    }

    uint32_t buffer = 0;
    address_space_read(&address_space_memory, desc + 4, MEMTXATTRS_UNSPECIFIED,
                       &buffer, sizeof(buffer));
    if (!buffer) {
        return;
    }
    /* A header, then the packet, then its check sequence.
     *
     * The header is forty-eight bytes, and that is read off the ROM rather
     * than chosen: on the branch at 0x40041231 the receive path computes
     * `buffer + 48 + n` and reads a halfword there, so the packet starts at
     * forty-eight. Before it, the same routine reads word 0, word 1, word 2
     * and the words at +44 and +47, so the header has at least twelve words
     * and five of them are looked at.
     *
     * What each one carries is only partly established, so the model writes
     * what the code makes plain and BOARDEMUL_WIFI_HEADER overrides any word
     * of it as `offset:value` pairs -- which turns each remaining guess into
     * a run rather than a rebuild. */
    uint8_t staged[RX_HEADER + FRAME_MAX + 4] = { 0 };
    uint8_t frame[FRAME_MAX];
    uint32_t header[RX_HEADER / 4] = { 0 };

    /* A beacon has to say it is on the channel it arrived on. The driver
     * compares the two and drops the frame when they differ -- read off a step
     * trace at 0x420c5998, where the beacon's own channel byte is compared
     * with the receive channel and an unequal pair returns -1 all the way back
     * to the buffer being recycled.
     *
     * The number comes from wDevCtrl+44, which is where the ROM's own receive
     * path takes it from when it stamps the channel into the header, so it is
     * the controller's notion of the channel it is tuned to. Reading driver
     * memory from a device is a shortcut, and an honest one only because the
     * alternative is not available: the channel is programmed through the
     * radio's front-end windows, which this project models in Python, and this
     * device cannot see them. BOARDEMUL_WIFI_CHANNEL, or the channel property,
     * pins it instead. */
    /* An answer the station is waiting for goes first; a beacon is what there
     * is to say when nothing was asked. */
    size_t frame_len;
    if (s->pending_len) {
        memcpy(frame, s->pending, s->pending_len);
        frame_len = s->pending_len;
        s->pending_len = 0;
    } else {
        frame_len = build_beacon(frame, s->ssid ? s->ssid : "BoardEmul",
                                 wifi_channel(s), s->wpa2);
    }
    uint32_t fcs = frame_check(frame, frame_len);

    /* The frame's length, in the two places the stack reads it.
     *
     * Word 1, bits 8..19, is the field `wDev_ProcessRxSucData` copies into the
     * low twelve bits of the word at +44 -- so that field is the length. The
     * copy only happens on one of its two branches, and the flash routine that
     * parses the frame reads +44 and drops anything shorter than sixteen
     * bytes. So on the other branch the hardware must be filling +44 itself,
     * and this writes both. Found by stepping the flash routine: with +44 left
     * at zero the frame was recycled unparsed; with the length there it is
     * read as a beacon, addresses and all.
     *
     * The count includes the check sequence, which is what the descriptor's
     * own length says arrived. */
    uint32_t length = (uint32_t)frame_len + 4u;
    header[1] = length << 8;
    header[11] = length & 0xFFFu;
    /* Word 0, bits 28 and 29. The flash routine that dispatches on the frame's
     * type reads word 0, ands it with 0x30000000, and goes nowhere useful if
     * the result is zero -- so at least one of them has to be set for a frame
     * to be looked at at all. What each one means is not established, which is
     * why both are set and why this is a measured requirement rather than a
     * decoded field. Without it the driver takes every frame and no scan ever
     * reports a network. */
    header[0] = 0x30000000u;
    for (unsigned i = 0; i < RX_HEADER / 4; i++) {
        if (s->header_set & (1u << i)) {
            header[i] = s->header_words[i];
        }
    }
    memcpy(staged, header, sizeof(header));
    staged[RX_HEADER_TAG] = (uint8_t)s->header_tag;
    memcpy(staged + RX_HEADER, frame, frame_len);
    memcpy(staged + RX_HEADER + frame_len, &fcs, sizeof(fcs));
    size_t staged_len = RX_HEADER + frame_len + 4;
    address_space_write(&address_space_memory, buffer, MEMTXATTRS_UNSPECIFIED,
                        staged, staged_len);
    /* Keep the size the driver gave the buffer; say how much arrived, mark it
     * the end of the frame, and hand it back by clearing the owner bit. */
    uint32_t filled = (word & DESC_SIZE_MASK)
                    | ((uint32_t)staged_len << DESC_LENGTH_SHIFT)
                    | DESC_EOF;
    address_space_write(&address_space_memory, desc, MEMTXATTRS_UNSPECIFIED,
                        &filled, sizeof(filled));
    s->delivered_to = desc;
    s->deliveries++;
    /* A standing access point never reaches the summary below, so it says how
     * it is getting on as it goes. Taken means the driver re-armed the
     * descriptor, which is the only thing the model can see. */
    if (!s->repeat && s->deliveries % 100 == 0) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: %u beacon(s) put on the air, "
                      "the driver took %u, and it has sent %u frame(s)%c",
                      s->deliveries, s->taken, s->transmits, 10);
    }
    if (s->deliveries <= 2) {
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: put %u byte(s) into the buffer at "
                  "0x%08x of descriptor 0x%08llx, word 0x%08x -> 0x%08x, and "
                  "raised interrupt %d%c", (unsigned)staged_len, buffer,
                  (unsigned long long)desc, word, filled, s->irq_source, 10);
    }
    s->regs[R_INT_STATUS / 4] = s->status_mask;
    s->regs[R_RX_LAST / 4] = (uint32_t)desc;
    s->regs[R_RX_QUEUES / 4] = s->queue_mask & RX_QUEUE_MASK;
    /* Armed once, on the first frame that actually goes on, and then left to
     * run out. Reassigning it per delivery -- which this did -- switches the
     * trace off again a beacon later, and nine accesses is not a trace. */
    if (s->deliveries == 1 && !s->trace_from_start) {
        s->trace_left = s->trace_window;
    }
    wifi_raise(s, 1);
    /* Again after a gap. It has to be long enough for the driver to hand the
     * descriptor back -- at a tenth of a second it ran the ring dry after two
     * frames -- and short enough to keep landing inside a scan's dwell on each
     * channel. BOARDEMUL_WIFI_GAP sets it, in milliseconds. */
    timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->gap_ns);
}

/* Which code is doing the asking. A driver that reads a register once has been
 * told something; a driver that reads it a thousand times from the same
 * instruction is in a loop waiting for an answer it is not getting, and the
 * two are indistinguishable without the program counter (ADR 0019). */
static void wifi_trace(EspWifiMac *s, const char *what, hwaddr offset,
                       uint32_t value)
{
    if (!s->trace_left) {
        return;
    }
    s->trace_left--;
    uint64_t pc = 0;
    if (current_cpu) {
        pc = CPU_GET_CLASS(current_cpu)->get_pc(current_cpu);
    }
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: after the frame, %s +0x%03x "
                  "= 0x%08x from pc 0x%08" PRIx64 "%c", what,
                  (unsigned)offset, value, pc, 10);
}

static uint64_t wifi_read(void *opaque, hwaddr offset, unsigned size)
{
    EspWifiMac *s = opaque;
    uint32_t value = offset < PAGE_SIZE ? s->regs[offset / 4] : 0;

    if (s->gated && offset == R_MAC_RESET) {
        value |= MAC_RESET_ACK;
    }
    wifi_trace(s, "rd", offset, value);
    return value;
}

/* The frame a transmit slot was armed with, out of the queue entry. */
static size_t wifi_read_tx_frame(unsigned slot, uint8_t *out, size_t max)
{
    uint32_t buffer = 0, at = 0;
    uint16_t head = 0, body = 0;

    address_space_read(&address_space_memory,
                       LMAC_QUEUES + slot * LMAC_QUEUE_STRIDE,
                       MEMTXATTRS_UNSPECIFIED, &buffer, sizeof(buffer));
    if (!buffer) {
        return 0;
    }
    address_space_read(&address_space_memory, buffer + EB_FRAME,
                       MEMTXATTRS_UNSPECIFIED, &at, sizeof(at));
    address_space_read(&address_space_memory, buffer + EB_HEAD_LEN,
                       MEMTXATTRS_UNSPECIFIED, &head, sizeof(head));
    address_space_read(&address_space_memory, buffer + EB_BODY_LEN,
                       MEMTXATTRS_UNSPECIFIED, &body, sizeof(body));
    size_t length = (size_t)head + body;
    if (!at || length < 24 || length > max) {
        /* The header is all that is read from it, so a length that does not
         * look right is no reason to give up on the frame. */
        length = 24;
    }
    if (address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                           out, length) != MEMTX_OK) {
        return 0;
    }
    return length;
}

/* What to send back, if anything. The three frames a joining station waits
 * for an answer to are a probe request, an authentication and an association
 * request, and each answer is the shortest one that accepts:
 *
 *   probe request       -> a probe response, which says what a beacon says
 *   authentication      -> open system, transaction two, success
 *   association request -> accepted, with an association identifier
 *
 * The subtype comes out of the request's first octet, and the reply is
 * addressed to whoever sent it. Frames that are neither management nor
 * addressed to this access point are not answered. */
static size_t wifi_build_reply(EspWifiMac *s, const uint8_t *request,
                               size_t length, uint8_t *out)
{
    if (length < 24 || (request[0] & 0x0C) != 0) {
        return 0;
    }
    const uint8_t *to = request + 4;                    /* the request's addr1 */
    if (memcmp(to, ap_address, 6) != 0 && memcmp(to, everyone, 6) != 0) {
        return 0;
    }
    const uint8_t *station = request + 10;              /* and its addr2 */
    size_t at;

    switch (request[0] & 0xF0) {
    case 0x40:                                          /* probe request */
        at = build_header(out, 0x50, station, s->sequence++);
        return at + build_body(out + at, s->ssid ? s->ssid : "BoardEmul",
                               wifi_channel(s), s->wpa2);
    case 0xB0:                                          /* authentication */
        at = build_header(out, 0xB0, station, s->sequence++);
        out[at++] = 0x00;                               /* open system */
        out[at++] = 0x00;
        out[at++] = 0x02;                               /* transaction two */
        out[at++] = 0x00;
        out[at++] = 0x00;                               /* success */
        out[at++] = 0x00;
        return at;
    case 0x00:                                          /* association */
        at = build_header(out, 0x10, station, s->sequence++);
        out[at++] = s->wpa2 ? 0x11 : 0x01;              /* the same capability */
        out[at++] = 0x00;
        out[at++] = 0x00;                               /* success */
        out[at++] = 0x00;
        out[at++] = 0x01;                               /* identifier one */
        out[at++] = 0xC0;
        out[at++] = 0x01;                               /* the rates it may use */
        out[at++] = 0x04;
        out[at++] = 0x82;
        out[at++] = 0x84;
        out[at++] = 0x8B;
        out[at++] = 0x96;
        return at;
    default:
        return 0;
    }
}

/* Which transmit slot a register offset belongs to, or -1. The array runs
 * downwards from R_TX_SLOT0 in eight-byte steps. */
static int wifi_tx_slot(hwaddr offset)
{
    if (offset > R_TX_SLOT0 || ((R_TX_SLOT0 - offset) % 8) != 0) {
        return -1;
    }
    unsigned slot = (R_TX_SLOT0 - offset) / 8;
    return slot < TX_SLOTS ? (int)slot : -1;
}

/* The frame has gone. A real MAC clears the two bits the driver set to arm the
 * slot and raises its transmit-complete source; the driver's handler routes
 * that bit to `lmacPostTxComplete`, which is a thunk for `pp_post(23)`, and
 * ppTask then ends the frame exchange and marks the queue idle again. Without
 * this the first frame is the last one. */
static void wifi_tx_complete(void *opaque)
{
    EspWifiMac *s = opaque;

    if (!s->tx_pending) {
        return;
    }
    for (unsigned slot = 0; slot < TX_SLOTS; slot++) {
        if (!(s->tx_pending & (1u << slot))) {
            continue;
        }
        s->regs[(R_TX_SLOT0 - slot * 8) / 4] &= ~TX_ARMED;
        /* The frame has left; if it wanted an answer, that is what an access
         * point is for. Only one waits at a time: the station sends one frame
         * and waits for its reply before sending the next. */
        uint8_t request[FRAME_MAX];
        size_t length = wifi_read_tx_frame(slot, request, sizeof(request));
        if (!length || s->pending_len) {
            continue;
        }
        size_t reply = wifi_build_reply(s, request, length, s->pending);
        if (!reply) {
            continue;
        }
        s->pending_len = reply;
        s->answers++;
        unsigned kind = (unsigned)(request[0] >> 4) & 0xFu;
        bool fresh = !(s->kinds & (1u << kind));
        s->kinds |= (uint16_t)(1u << kind);
        if (s->answers <= 8 || fresh) {
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the station sent a 0x%02x "
                          "from %02x:%02x:%02x:%02x:%02x:%02x -- answering with "
                          "a 0x%02x of %u byte(s)%c", request[0],
                          request[10], request[11], request[12], request[13],
                          request[14], request[15], s->pending[0],
                          (unsigned)reply, 10);
        }
        /* Put it on soon, but not in the same instant as the
         * transmit-complete interrupt: on the air the frame leaves, and the
         * answer comes back after it. */
        timer_mod_ns(s->hatch,
                     qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->tx_delay_ns);
    }
    s->regs[R_TX_DONE_SLOTS / 4] =
        (s->regs[R_TX_DONE_SLOTS / 4] & ~TX_DONE_MASK)
        | (s->tx_pending & TX_DONE_MASK);
    s->tx_pending = 0;
    s->regs[R_INT_STATUS / 4] |= TX_DONE_STATUS;
    wifi_raise(s, 1);
}

static void wifi_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    EspWifiMac *s = opaque;

    if (offset >= PAGE_SIZE) {
        return;
    }
    /* Traced before the special cases return, because the two registers with
     * their own behaviour are the two most worth watching -- and they were the
     * two the trace could not see, which cost a run. */
    wifi_trace(s, "wr", offset, (uint32_t)value);
    if (offset == R_RX_COMMAND) {
        s->regs[offset / 4] = (uint32_t)value & ~RX_COMMAND_GO;
        return;
    }
    if (offset == R_TX_DONE_ACK && s->beacon_ms) {
        /* Write one to clear, on the register beside it. A pending set that
         * never empties is a handler that never stops -- the same lesson the
         * interrupt status taught. */
        s->regs[R_TX_DONE_SLOTS / 4] &= ~((uint32_t)value & TX_DONE_MASK);
        s->regs[offset / 4] = (uint32_t)value;
        return;
    }
    if (offset == R_INT_CLEAR) {
        /* What the handler has dealt with. The line goes down when nothing is
         * left; a status that never clears is a handler that never stops. */
        s->regs[R_INT_STATUS / 4] &= ~(uint32_t)value;
        s->regs[offset / 4] = (uint32_t)value;
        if (!s->regs[R_INT_STATUS / 4]) {
            wifi_raise(s, 0);
        }
        return;
    }
    /* Completing transmissions is modelled only for a run that has asked for
     * an access point. This file's rule is that with nothing configured the
     * device is the trace window it replaced, byte for byte, and the rule
     * earned itself again here: switching completion on unconditionally broke
     * the two-node mesh test, deterministically and in the same place every
     * time -- Meshtastic's second node brings its station up, arms a slot, and
     * a transmit-complete interrupt it had never received before leaves it
     * unable to finish its run. Nothing in the mesh has any use for a
     * transmit that completes, so nothing in the mesh is given one. */
    int slot = s->beacon_ms ? wifi_tx_slot(offset) : -1;
    if (slot >= 0 && ((uint32_t)value & TX_ARMED) == TX_ARMED
        && (s->regs[offset / 4] & TX_ARMED) != TX_ARMED) {
        /* Armed now, by this write. Requiring the transition rather than the
         * value keeps a driver that reads the register and puts it back from
         * looking like a fresh frame. */
        s->regs[offset / 4] = (uint32_t)value;
        s->tx_pending |= 1u << slot;
        s->transmits++;
        if (s->transmits <= 4 || s->transmits % 100 == 0) {
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the driver transmitted on "
                          "slot %d (%u so far) -- control 0x%08x, parameters 0x%08x%c",
                          slot, s->transmits, (uint32_t)value,
                          s->regs[(R_TX_SLOT0 - 4 - slot * 8) / 4], 10);
        }
        timer_mod_ns(s->tx_timer,
                     qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->tx_delay_ns);
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
    s->tx_pending = 0;
    s->pending_len = 0;
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
    const char *tag = getenv("BOARDEMUL_WIFI_TAG");
    s->header_tag = tag ? (uint32_t)strtoul(tag, NULL, 0) : 64;
    const char *queues = getenv("BOARDEMUL_WIFI_QUEUE");
    s->queue_mask = queues ? (uint32_t)strtoul(queues, NULL, 0) : 1;
    const char *gap = getenv("BOARDEMUL_WIFI_GAP");
    int64_t period = gap ? strtoll(gap, NULL, 0)
                         : (s->beacon_ms ? (int64_t)s->beacon_ms : 500);
    s->gap_ns = period * 1000000LL;
    const char *many = getenv("BOARDEMUL_WIFI_REPEAT");
    /* Zero is "keep going", which is what an access point does. */
    s->repeat = many ? (unsigned)strtoul(many, NULL, 0)
                     : (s->beacon_ms ? 0u : 1u);
    /* offset:value pairs, comma separated, offsets in bytes within the
     * header. Parsed here rather than per frame so a malformed value is
     * reported once. */
    const char *words = getenv("BOARDEMUL_WIFI_HEADER");
    if (words) {
        char *dup = g_strdup(words);
        for (char *tok = strtok(dup, ","); tok; tok = strtok(NULL, ",")) {
            char *colon = strchr(tok, ':');
            if (!colon) {
                continue;
            }
            *colon = 0;
            unsigned long at = strtoul(tok, NULL, 0);
            if (at % 4 || at >= RX_HEADER) {
                error_report("esp-wifi-mac: header offset %lu is not a word "
                             "inside the header", at);
                continue;
            }
            s->header_words[at / 4] = (uint32_t)strtoul(colon + 1, NULL, 0);
            s->header_set |= 1u << (at / 4);
        }
        g_free(dup);
    }
    const char *pinned = getenv("BOARDEMUL_WIFI_CHANNEL");
    if (pinned) {
        s->channel = (uint32_t)strtoul(pinned, NULL, 0);
    }
    /* Normally the trace window opens on the first frame delivered, which is
     * after the station is up. BOARDEMUL_WIFI_TRACE_AT opens it at once
     * instead, for the questions that are about what the driver does before
     * it can hear anything -- the transmit path among them. */
    s->trace_from_start = getenv("BOARDEMUL_WIFI_TRACE_AT") != NULL;
    const char *window = getenv("BOARDEMUL_WIFI_TRACE");
    s->trace_window = window ? (unsigned)strtoul(window, NULL, 0) : 120;
    if (s->trace_from_start) {
        s->trace_left = s->trace_window;
    }
    const char *mask = getenv("BOARDEMUL_WIFI_STATUS");
    s->status_mask = mask ? (uint32_t)strtoul(mask, NULL, 0) : RX_DONE_STATUS;
    /* How long a frame takes to leave. A short management frame at the lowest
     * rate is a few hundred microseconds of air; the exact number is not
     * established, so BOARDEMUL_WIFI_TX_US sets it. */
    const char *air = getenv("BOARDEMUL_WIFI_TX_US");
    s->tx_delay_ns = (air ? (int64_t)strtoll(air, NULL, 0) : 300) * 1000LL;
    s->tx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, wifi_tx_complete, s);
    s->hatch = timer_new_ns(QEMU_CLOCK_VIRTUAL, wifi_deliver, s);
    qemu_register_reset(wifi_reset, s);
    if (after || s->beacon_ms) {
        /* Either the investigation hatch -- how many seconds into the run to
         * deliver one -- or a standing access point, which starts beaconing
         * once the driver has had time to bring its station up. Nothing is
         * lost by starting before that: a delivery with no ring handed over
         * yet says so and comes back. */
        double first = after ? atof(after) : (double)s->beacon_ms / 1000.0;
        timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                     + (int64_t)(first * 1000000000.0));
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
    /* An access point in the air: what it is called, how often it beacons in
     * milliseconds (zero for none), and which channel it claims (zero to
     * claim whichever the controller is tuned to). */
    DEFINE_PROP_STRING("ssid", EspWifiMac, ssid),
    DEFINE_PROP_UINT32("beacon-ms", EspWifiMac, beacon_ms, 0),
    DEFINE_PROP_UINT32("channel", EspWifiMac, channel, 0),
    DEFINE_PROP_BOOL("wpa2", EspWifiMac, wpa2, false),
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
