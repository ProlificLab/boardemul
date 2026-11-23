/*
 * SPDX-FileCopyrightText: 2026 Prolific Lab
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
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
#include "qemu/cutils.h"
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

#include <sys/socket.h>
#include <sys/un.h>
#include <gcrypt.h>

#define TYPE_ESP_WIFI_MAC "esp-wifi-mac"
OBJECT_DECLARE_SIMPLE_TYPE(EspWifiMac, ESP_WIFI_MAC)

/* Two pages, not one. The transmit result the flash driver reads after every
 * frame is at +0x1320, in the page above the register block, so a device that
 * claims only the first page leaves the last word of its own transmit path to
 * an unmodelled window. See R_TX_RESULT0. */
#define REG_SPAN        0x2000
#define REG_COUNT       (REG_SPAN / 4)
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
/* What every received frame's strength reads as: a station across a room from
 * its access point. The WiFi air has no positions to work one out from (ADR
 * 0177). */
#define RX_RSSI_DBM     (-45)
#define RX_HEADER_TAG   8
#define R_RX_QUEUES     0xca8
/* Whether the MAC is running at all. ic_mac_deinit (ROM, 0x4003c5fc) spins
 * while bits 13 and 14 are set and then ORs 0x00ff1000 in; ic_mac_init
 * (0x4003c5e0) ANDs with 0xff00efff, which clears exactly those. So the eight
 * bits at 16..23 and bit 12 set is the MAC held, and nothing is received then. */
#define R_MAC_HOLD      0xca0
#define MAC_HELD        0x00ff1000u
/* And two bits the driver waits on before it holds the MAC: ic_mac_deinit
 * (0x4003c5fc) spins on `+0xca0 & 0x6000` until both are clear, holds, and
 * spins on them again. A real MAC raises them while a frame is on the air,
 * so the driver never changes channel under a transmission. Without them the
 * hold lands before the frame has left, and the frame that the last channel
 * of a scan was sent on is never completed: its queue stays busy, and every
 * send after it -- every later scan's probes, the authentication -- is
 * abandoned unsent. */
#define MAC_BUSY        0x00006000u
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
/* The eb also holds, at offset four, a pointer to the DMA descriptor (a
 * standard ESP lldesc) the hardware transmits from; the descriptor's second
 * word is the frame's buffer address. This is the authoritative frame pointer
 * -- EB_FRAME above only coincides with it for management frames (ADR 0069). */
#define EB_DESC           4
#define LLDESC_BUF        4
/* The most of a frame this device ever reads or writes. */
/* And how the driver says it has dealt with them. Found the same way, one
 * answer at a time: with the mask left at zero the handler read it and
 * stopped; with the completed slot in it, the handler read the mask, then read
 * this register -- which nothing had ever touched before -- and wrote the same
 * slot back. That is the ordinary split of a pending set and its
 * acknowledgement, the same shape as the interrupt status at +0xc3c and its
 * acknowledgement at +0xc40 a few registers along. */
#define R_TX_DONE_ACK   0xcac
/* What the frame did. One seventy-six byte block per slot, counting *down*
 * from slot 0, and the driver reads bits 12..15 of the first word: the flash
 * `lmacProcessTxComplete` jumps through a six-entry table on them --
 *
 *     0  lmacProcessTxSuccess      4  lmacProcessTxError
 *     1  lmacProcessTxRtsError     5  lmacProcessAckTimeout
 *     2  lmacProcessCtsTimeout     3  an assert that aborts the firmware
 *
 * read off the table at 0x3c18cbc0 and the function names the driver carries
 * for its own asserts. So zero is success -- which is what an unmodelled
 * window was already returning, by accident. This device says it on purpose,
 * per slot, at the moment the frame completes -- success when the access point
 * acknowledged the frame or it was group-addressed, an acknowledgement timeout
 * when nothing did (ADR 0109). The remaining fields are left at zero: bits
 * 16..23 are the only others the success path is handed, and it does not read
 * them. */
#define R_TX_RESULT0      0x1320
#define TX_RESULT_STRIDE  76
#define TX_RESULT_SUCCESS 0x00000000u
/* Bits 12..15 of 5: lmacProcessAckTimeout, for a frame nobody acknowledged
 * (ADR 0109). */
#define TX_RESULT_ACK_TIMEOUT 0x00005000u
/* A hatch, not hardware: somewhere in this device's span the driver never
 * touches, used to hand a debugger a trigger. With BOARDEMUL_WIFI_MARK set
 * to a subtype, the device writes that subtype here when the station sends
 * one, and holds *that* answer back for a while so a debugger polling the cell
 * has time to arm a breakpoint before the frame lands. That turns a step trace
 * of one chosen received frame from a breakpoint on every frame -- which slows
 * the guest fifty-fold, so it never reaches an authentication at all -- into a
 * single stop at the right one.
 *
 * The hold is a delay and not a handshake because a debugger cannot release
 * one: QEMU puts gdb's memory writes through `address_space_write_rom`, which
 * reaches memory-backed regions only, so a write to a device register from a
 * debugger is accepted and silently dropped. Reads are fine, which is why the
 * cell is polled rather than cleared.
 *
 * Off unless the variable is set, and the cell is inert either way. */
#define R_PROBE_GATE      0x1ff8
#define PROBE_HOLD_MS     500

/* The frame channel (ADR 0064). This device is the radio and the driver's view
 * of it; what is on the air is somebody else's business, and that somebody is
 * a Python peer on a socket. Synchronous request and reply, which is the shape
 * the bus bridge already establishes:
 *
 *     request   type:u16 channel:u16 length:u16 clock:u32 free:u16 frame[]
 *     reply     length:u16                                          frame[]
 *
 * A reply of zero length means nothing to say. Two request types: the guest
 * transmitted this frame, and a tick.
 *
 * The clock is the guest's, in milliseconds, and it is what keeps the schedule
 * on the guest's clock while leaving the peer to decide what is due. The
 * device used to decide instead -- every firing with nothing staged put a
 * beacon on -- which meant an answer earned an *extra* beacon rather than
 * merely not costing one, and four hundred answers bought four hundred
 * beacons the interval never asked for. That filled the driver's four
 * descriptors faster than it drained them, and the second that mattered --
 * between an association response and the handshake message that should
 * follow it -- was a second with no free descriptor and nothing on the air.
 * So the device offers opportunities and says what time it is; the peer says
 * what, if anything, is due. */
#define FRAME_TX          1u
#define FRAME_TICK        2u
/* Or-ed into the type of the next request when the last frame the peer offered
 * on a tick reached a receive descriptor: the model's transmit acknowledgement.
 * The peer uses it the way hostapd uses an acknowledged message one -- to stop
 * retrying it at once (ADR 0095). */
#define FRAME_DELIVERED   0x8000u
/* The peer's only reply flag: a station is waiting for this frame, so it may
 * go on the air even while the receive-enable bit reads clear. It does not
 * excuse a held MAC -- that is a radio tuned elsewhere, and a frame delivered
 * into it never happened. ADR 0068. */
#define REPLY_AWAITED     0x0001u
/* And a second: the sixteen octets between the reply header and the frame are
 * the pairwise temporal key, handed over so this device can encrypt and decrypt
 * data frames the way the hardware crypto engine does (ADR 0094). The key
 * install never reaches MMIO for the device to read (ADR 0077), so the peer --
 * which derived the same key from the handshake -- sends it here instead. */
#define REPLY_KEY         0x0002u
/* And a third: the access point did not acknowledge the frame it was just
 * handed -- an individually addressed frame not addressed to it. The slot then
 * completes with an acknowledgement timeout instead of success (ADR 0109). */
#define REPLY_UNACKED     0x0004u
/* Or-ed into the type of the next request when the last frame the peer offered
 * was retried to the limit and never acknowledged, as hostapd hears a failed
 * tx_status (ADR 0109). */
#define FRAME_UNACKED     0x4000u
/* The 802.11 retry rules for a 2.4 GHz OFDM network (IEEE Std 802.11-2016):
 * dot11ShortRetryLimit attempts in all; before each retry the sender waits its
 * ACK timeout (SIFS 10 + slot 9 + PHY start delay 25 us) and DIFS (28 us), then
 * a backoff from a contention window that starts at 15 slots and doubles to
 * 1023. The backoff is taken at its mean, so a run stays reproducible. */
#define RETRY_LIMIT       7
#define ACK_TIMEOUT_US    44
#define DIFS_US           28
#define SLOT_US           9
#define CW_MIN            15
#define CW_MAX            1023
/* The Retry bit, in the second octet of the frame control field. */
#define FC1_RETRY         0x08u
/* Answers waiting behind the one being delivered. The access point's radio
 * hears every frame the station sends; its answers queue (ADR 0109). */
#define ANSWER_BACKLOG    4
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

/* What a frame looks like is the peer's business now (ADR 0064): the beacon
 * builder, the body builder and the reply builder all left this file with it.
 * What stays is the size of the biggest frame this radio will carry, because
 * that bounds the buffers below.
 *
 * Sixteen hundred, not the hundred and twenty-eight a beacon needs. That bound
 * came from the beacon builder and outlived it by one commit: the first
 * handshake message is a hundred and thirty-one bytes, so the radio refused it
 * and closed the channel, and the station waited for a frame that had been
 * thrown away at this end. The third message with a wrapped group key is
 * bigger again, and a data frame carrying a DHCP exchange is bigger than
 * that. */
#define FRAME_MAX         1600

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
    /* A data-frame length that opens the window instead, when a frame of
     * exactly that length is delivered -- 187 is the handshake's message
     * three, and the second after it is where the station falls silent.
     * BOARDEMUL_WIFI_TRACE_DATA. ADR 0075. */
    unsigned trace_on_data;
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
    /* What the peer offered on this pass, which is not the same thing as
     * the staged answer above and must never become a queue. An answer
     * waits for the radio and is retried; a frame the peer started is on
     * the air or it is lost, so this slot is filled and emptied inside one
     * pass of wifi_deliver. ADR 0067. */
    uint8_t offered[FRAME_MAX];
    size_t offered_len;
    bool offered_awaited;
    unsigned dropped;
    uint16_t sequence;
    unsigned answers;
    /* One bit per management subtype the station has sent. A fixed cap on
     * the transmit log read as "the station only ever sent four frames",
     * which was a cap and not a measurement, and it sent the reasoning
     * that followed it wrong. Every new kind of frame is reported. */
    uint16_t kinds;
    /* The access point's channel, the only one it is heard on and hears on
     * (ADR 0110). The station hears it only while tuned there. */
    uint32_t ap_channel;
    /* The access point this device puts in the air, if any. */
    uint32_t beacon_ms;
    bool gated;
    uint32_t status_mask;
    uint32_t queue_mask;
    uint32_t header_tag;
    uint32_t mark_kind;
    bool any_descriptor;
    char *frames_path;
    /* Why a delivery did not happen, so the binding constraint is a number
     * rather than a guess: no ring, the MAC not listening, the head
     * descriptor still full, its buffer not yet pointed anywhere, the
     * station tuned away from the access point's channel. */
    unsigned stalled[6];
    /* Two delivery choices, switchable so the difference is measured rather
     * than argued. Defaults are what the C peer did and the suites pass on:
     * an answer skips the receive gate so it is prompt, and the beacon it
     * displaced follows it rather than waiting a whole period. Measured,
     * with nothing else changed: prompt and no follow-up gave thirty-three
     * silent scans in thirty-five and no association; gated with a
     * follow-up gave ten in fourteen; both together gave none in five, and
     * two associations. BOARDEMUL_WIFI_GATE_ANSWERS=1 and
     * BOARDEMUL_WIFI_NO_FOLLOWUP=1 turn each round the other way. */
    bool answer_exempt;
    bool answer_followup;
    /* Descriptors delivered into and not yet seen handed back. */
    hwaddr awaiting[DESC_MAX];
    int frames_fd;
    /* The pairwise temporal key, once the peer has handed it over (ADR 0094),
     * and whether it has. Until then data frames cross the air in the clear,
     * which is the identity cipher ADR 0084 lived with. */
    uint8_t tk[16];
    bool have_key;
    /* The last frame the peer offered reached a descriptor, and the peer has
     * not been told yet (ADR 0095). */
    bool offered_delivered;
    /* The last frame the peer offered was retried to the limit unacknowledged
     * (ADR 0109), and the peer has not been told yet. */
    bool offered_unacked;
    /* How many times the frame being delivered has been tried and not taken:
     * the offered one and the answer, each its own count (ADR 0109). */
    unsigned offered_attempts;
    unsigned answer_attempts;
    unsigned retried, gave_up;
    /* Answers behind the one in `pending` (ADR 0109). */
    uint8_t backlog[ANSWER_BACKLOG][FRAME_MAX];
    size_t backlog_len[ANSWER_BACKLOG];
    unsigned backlog_count;
    int64_t mark_at;
    uint32_t ring;              /* what the driver handed us */
    bool said_no_ring;          /* the wait for it has been logged */
    unsigned deliveries;
    unsigned taken;
    unsigned repeat;
    int64_t gap_ns;
};

/* How long to wait before trying to put the staged frame on again. An answer
 * is already in flight on the air, so it waits its own air time and no more;
 * a beacon has nothing to be late for and waits for the next beacon. Getting
 * this wrong on two of the three gates below delayed an authentication reply
 * by a whole beacon period -- a full second on a run with beacons turned
 * down, which is an eternity for a frame the standard answers in
 * microseconds. */
static int64_t wifi_retry_ns(EspWifiMac *s)
{
    return s->pending_len ? s->tx_delay_ns : s->gap_ns;
}

/* How long the access point waits after `attempts` unacknowledged tries before
 * the next one: its ACK timeout, DIFS, and the mean backoff of the window for
 * that attempt (ADR 0109). */
static int64_t wifi_backoff_ns(unsigned attempts)
{
    unsigned cw = CW_MIN;

    for (unsigned i = 0; i < attempts && cw < CW_MAX; i++) {
        cw = cw * 2 + 1;
    }
    if (cw > CW_MAX) {
        cw = CW_MAX;
    }
    return (int64_t)(ACK_TIMEOUT_US + DIFS_US) * 1000 + (int64_t)cw * SLOT_US * 500;
}

/* The next queued answer takes the place of one that has gone, or given up. */
static void wifi_next_answer(EspWifiMac *s)
{
    s->pending_len = 0;
    s->answer_attempts = 0;
    if (!s->backlog_count) {
        return;
    }
    memcpy(s->pending, s->backlog[0], s->backlog_len[0]);
    s->pending_len = s->backlog_len[0];
    s->backlog_count--;
    memmove(s->backlog[0], s->backlog[1], s->backlog_count * sizeof(s->backlog[0]));
    memmove(s->backlog_len, s->backlog_len + 1,
            s->backlog_count * sizeof(s->backlog_len[0]));
}

/* A standing access point never reaches the repeat summary, so it says how it
 * is getting on as it goes: every hundred frames the peer put on the air,
 * whether the station's radio took them or they were lost. Counted on both,
 * not on deliveries alone, because a station that scans is on the access
 * point's channel for a fraction of each sweep (ADR 0110), and a hundred
 * deliveries can take longer than a whole run. Taken means the driver re-armed
 * the descriptor, which is the only thing the model can see. */
static void wifi_progress(EspWifiMac *s)
{
    if (s->repeat || (s->deliveries + s->dropped) % 100) {
        return;
    }
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: %u frame(s) put on the air, "
                  "the driver took %u, and it has sent %u; stalled on "
                  "no ring %u, off channel %u, mac held %u, receiver off "
                  "%u, no free descriptor %u, no buffer %u; %u frame(s) the peer "
                  "offered were lost to a closed radio; %u retr(ies), "
                  "%u frame(s) given up unacknowledged%c",
                  s->deliveries, s->taken, s->transmits,
                  s->stalled[0], s->stalled[5], s->stalled[1], s->stalled[4],
                  s->stalled[2], s->stalled[3], s->dropped,
                  s->retried, s->gave_up, 10);
}

/* The station's radio did not take the frame being delivered: held, deaf, or
 * out of buffers. To an individually addressed frame that is a missing
 * acknowledgement, and the access point tries it again with the Retry bit set,
 * up to the limit; a group frame is sent once and is simply gone. Returns the
 * delay before the retry, or zero when there is nothing left to retry. */
static int64_t wifi_not_taken(EspWifiMac *s)
{
    bool answer = s->pending_len != 0;
    uint8_t *frame = answer ? s->pending : s->offered;
    size_t len = answer ? s->pending_len : s->offered_len;
    unsigned *attempts = answer ? &s->answer_attempts : &s->offered_attempts;

    if (!len) {
        return 0;
    }
    if (len < 10 || (frame[4] & 1)) {
        if (!answer) {
            s->offered_len = 0;
            s->dropped++;
            wifi_progress(s);
        }
        return 0;
    }
    if (++*attempts < RETRY_LIMIT) {
        frame[1] |= FC1_RETRY;
        s->retried++;
        return wifi_backoff_ns(*attempts);
    }
    s->gave_up++;
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms a 0x%02x of %u byte(s) to "
                  "%02x:%02x:%02x:%02x:%02x:%02x went unacknowledged %u time(s); "
                  "given up%c",
                  (unsigned)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000),
                  frame[0], (unsigned)len, frame[4], frame[5], frame[6],
                  frame[7], frame[8], frame[9], *attempts, 10);
    if (answer) {
        wifi_next_answer(s);
    } else {
        s->offered_len = 0;
        s->offered_attempts = 0;
        s->offered_unacked = true;
        s->dropped++;
        wifi_progress(s);
    }
    return 0;
}

static void wifi_raise(EspWifiMac *s, int level)
{
    if (s->irq_line && level != s->irq_level) {
        s->irq_level = level;
        qemu_set_irq(s->irq_line, level);
    }
}

/* Follow the chain to the first descriptor the hardware still owns, which is
 * the one a real MAC would fill next. */
/* Defined below, beside the walk it shares its reading of the chain with. */
static unsigned wifi_free_count(EspWifiMac *s);

/* Real CCMP (ADR 0094). The pairwise data cipher lives here, in the MAC, the
 * way the hardware crypto engine's does: the body of a Protected data frame is
 * encrypted before it goes on the air and decrypted when it comes off, while the
 * driver only ever sees plaintext. The key is the peer's -- the same key it
 * derived from the handshake, since the install never reaches MMIO (ADR 0077).
 * The nonce and additional-authenticated-data are built exactly as emu/ccmp.py
 * builds them, so the two ends agree, and to 802.11-2016 so a capture decodes.
 * This network has no fourth-address (WDS) frames, so that case is left out. */

static size_t ccmp_hdr_len(const uint8_t *f)
{
    return ((f[0] & 0x0C) == 0x08 && (f[0] & 0x80)) ? 26 : 24;   /* QoS: +2 */
}

static bool ccmp_is_protected_data(const uint8_t *f, size_t len)
{
    return (f[0] & 0x0C) == 0x08 && (f[1] & 0x40)
        && len >= ccmp_hdr_len(f) + 8;
}

static uint64_t ccmp_pn(const uint8_t *h)
{
    return (uint64_t)h[0] | ((uint64_t)h[1] << 8) | ((uint64_t)h[4] << 16)
        | ((uint64_t)h[5] << 24) | ((uint64_t)h[6] << 32) | ((uint64_t)h[7] << 40);
}

static void ccmp_nonce(const uint8_t *f, size_t hdrlen, uint64_t pn, uint8_t out[13])
{
    uint8_t priority = (hdrlen == 26) ? (f[24] & 0x0F) : 0;   /* the QoS TID */
    out[0] = priority;                          /* a data frame: no management bit */
    memcpy(out + 1, f + 10, 6);                 /* address two, the transmitter */
    for (int i = 0; i < 6; i++) {
        out[7 + i] = (uint8_t)(pn >> (8 * (5 - i)));   /* packet number, big-endian */
    }
}

static size_t ccmp_aad(const uint8_t *f, size_t hdrlen, uint8_t out[30])
{
    uint8_t fc0 = f[0] & ~0x70;                 /* a data frame: subtype bits masked */
    uint8_t fc1 = (uint8_t)((f[1] & ~0x38) | 0x40);  /* Retry/PM/MoreData off, Protected on */
    size_t n = 0;
    out[n++] = fc0;
    out[n++] = fc1;
    memcpy(out + n, f + 4, 18);                 /* A1, A2, A3 */
    n += 18;
    out[n++] = f[22] & 0x0F;                    /* Sequence Control, sequence masked */
    out[n++] = 0;
    if (hdrlen == 26) {
        out[n++] = f[24] & 0x0F;                /* QoS Control, only the TID kept */
        out[n++] = 0;
    }
    return n;
}

/* One CCM operation over gcrypt (the same libgcrypt esp_aes.c links), M=8, L=2,
 * matching emu/aes.py. For decryption, false means the MIC did not verify. */
static bool ccmp_ccm(const uint8_t *key, const uint8_t *nonce,
                     const uint8_t *aad, size_t aadlen,
                     uint8_t *data, size_t datalen, uint8_t *tag, bool encrypt)
{
    gcry_cipher_hd_t h;
    if (gcry_cipher_open(&h, GCRY_CIPHER_AES128, GCRY_CIPHER_MODE_CCM, 0)) {
        return false;
    }
    bool ok = false;
    uint64_t lengths[3] = { datalen, aadlen, 8 };
    if (gcry_cipher_setkey(h, key, 16)
        || gcry_cipher_setiv(h, nonce, 13)
        || gcry_cipher_ctl(h, GCRYCTL_SET_CCM_LENGTHS, lengths, sizeof(lengths))
        || gcry_cipher_authenticate(h, aad, aadlen)) {
        goto out;
    }
    if (encrypt) {
        ok = !gcry_cipher_encrypt(h, data, datalen, NULL, 0)
            && !gcry_cipher_gettag(h, tag, 8);
    } else {
        ok = !gcry_cipher_decrypt(h, data, datalen, NULL, 0)
            && !gcry_cipher_checktag(h, tag, 8);
    }
out:
    gcry_cipher_close(h);
    return ok;
}

/* Seal a frame before the air: encrypt the body in place and append its MIC,
 * returning the new length (eight more). A frame that is not a Protected data
 * frame, or one sent before the key arrived, is left as it is. The buffer must
 * have eight octets of room past `len`. */
static size_t wifi_ccmp_seal(EspWifiMac *s, uint8_t *f, size_t len)
{
    if (!s->have_key || !ccmp_is_protected_data(f, len)) {
        return len;
    }
    size_t hdr = ccmp_hdr_len(f);
    uint8_t nonce[13], aad[30];
    ccmp_nonce(f, hdr, ccmp_pn(f + hdr), nonce);
    size_t aadlen = ccmp_aad(f, hdr, aad);
    if (!ccmp_ccm(s->tk, nonce, aad, aadlen, f + hdr + 8, len - hdr - 8,
                  f + len, true)) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: CCMP seal failed%c", 10);
        return len;
    }
    return len + 8;
}

/* Open a frame off the air: verify its MIC and decrypt the body in place. The
 * length is left as it is, the eight-octet MIC still on the end -- the driver
 * strips it, exactly as it did the eight-zero placeholder the identity cipher
 * left there (ADR 0084), so what it reads is the frame's own body. A MIC that
 * does not check is logged and the frame left encrypted, so the driver rejects
 * it rather than this passing garbage on -- with both ends ours it should never
 * happen. */
static size_t wifi_ccmp_open(EspWifiMac *s, uint8_t *f, size_t len)
{
    if (!s->have_key || !ccmp_is_protected_data(f, len)) {
        return len;
    }
    size_t hdr = ccmp_hdr_len(f);
    if (len < hdr + 8 + 8) {
        return len;
    }
    size_t bodylen = len - hdr - 8 - 8;
    uint8_t nonce[13], aad[30];
    ccmp_nonce(f, hdr, ccmp_pn(f + hdr), nonce);
    size_t aadlen = ccmp_aad(f, hdr, aad);
    if (!ccmp_ccm(s->tk, nonce, aad, aadlen, f + hdr + 8, bodylen,
                  f + hdr + 8 + bodylen, false)) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: a data frame failed its MIC%c", 10);
        return len;
    }
    return len;
}

/* One request, one reply. Blocking, like every unmodelled access the bus
 * bridge forwards: the guest tolerates that already and this happens once per
 * beacon rather than once per load. A peer that has gone away closes the
 * channel and the device falls silent, which is what an access point being
 * switched off looks like. */
static size_t wifi_ask(EspWifiMac *s, uint16_t type, unsigned channel,
                       const uint8_t *frame, size_t len,
                       uint8_t *out, size_t max, uint16_t *flags)
{
    if (s->frames_fd < 0) {
        return 0;
    }
    /* A transmitted data frame is sealed before it goes on the air (ADR 0094):
     * the body is encrypted and a MIC appended, into a copy because the caller's
     * frame is const and grows by eight. Everything else goes as it is. */
    uint8_t sealed[FRAME_MAX + 16];
    if (type == FRAME_TX && frame && len && len <= FRAME_MAX && s->have_key
        && ccmp_is_protected_data(frame, len)) {
        memcpy(sealed, frame, len);
        len = wifi_ccmp_seal(s, sealed, len);
        frame = sealed;
    }
    uint8_t head[12];
    stw_le_p(head + 0, type | (s->offered_delivered ? FRAME_DELIVERED : 0)
                            | (s->offered_unacked ? FRAME_UNACKED : 0));
    s->offered_delivered = false;
    s->offered_unacked = false;
    stw_le_p(head + 2, (uint16_t)channel);
    stw_le_p(head + 4, (uint16_t)len);
    stl_le_p(head + 6, (uint32_t)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                  / 1000000));
    stw_le_p(head + 10, (uint16_t)wifi_free_count(s));
    if (qemu_write_full(s->frames_fd, head, sizeof(head)) != sizeof(head)
        || (len && qemu_write_full(s->frames_fd, frame, len) != len)) {
        goto gone;
    }
    /* length, then flags. One flag says a station is waiting for this frame
     * (ADR 0068); another says the sixteen octets that follow the header, before
     * the frame, are the pairwise key to install (ADR 0094). */
    uint8_t reply[4];
    if (recv(s->frames_fd, reply, sizeof(reply), MSG_WAITALL) != sizeof(reply)) {
        goto gone;
    }
    size_t back = lduw_le_p(reply);
    uint16_t reply_flags = lduw_le_p(reply + 2);
    if (flags) {
        *flags = reply_flags;
    }
    if (reply_flags & REPLY_KEY) {
        if (recv(s->frames_fd, s->tk, sizeof(s->tk), MSG_WAITALL)
            != (ssize_t)sizeof(s->tk)) {
            goto gone;
        }
        s->have_key = true;
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the pairwise key is in; data "
                      "frames are encrypted from here%c", 10);
    }
    if (!back) {
        return 0;
    }
    if (back > max) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the peer offered %u byte(s), "
                      "which is more than this radio can put on the air%c",
                      (unsigned)back, 10);
        goto gone;
    }
    if (recv(s->frames_fd, out, back, MSG_WAITALL) != (ssize_t)back) {
        goto gone;
    }
    /* A received data frame is opened: its MIC verified and its body decrypted,
     * so what reaches the driver is plaintext, as from a real MAC (ADR 0094). */
    return wifi_ccmp_open(s, out, back);

gone:
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the frame channel closed; "
                  "nothing more will go on the air%c", 10);
    close(s->frames_fd);
    s->frames_fd = -1;
    return 0;
}

/* The head of the list the consumer walks. `wdevProcessRxSucDataAll`
 * (0x4004147c) starts at `*wDevCtrl` and follows each descriptor's next
 * pointer at +8, counting as it goes:
 *
 *     a3 = 0
 *     loop: word = *desc; a3 += 1; next = *(desc + 8)
 *           if (!(word & (1 << 30))) goto skip     -- no frame here
 *           wDev_ProcessRxSucData(desc, a3); a3 = 0
 *
 * and that count is the second argument `wDev_IndicateFrame` tests against
 * one. So the count is not a queue and not an index: it is how many
 * descriptors were examined to reach this frame. One means "the next one in
 * order", and anything else means descriptors were skipped, which the ROM
 * treats as frames lost and throws the frame away.
 *
 * Which is why this model's scan worked and its authentication reply did not.
 * The walk below used to start from whichever descriptor the driver had most
 * recently handed back, so a frame landed at an arbitrary position in the
 * list: measured, a beacon at position one was delivered and an
 * authentication reply at position four was discarded. Beacons come ten a
 * second and enough of them drew a one; a reply gets one throw per connect
 * attempt.
 *
 * A real MAC does not choose. It writes into the next descriptor in order,
 * and if that one is still full it is out of room. So this starts where the
 * consumer starts. Reading `wDevCtrl` is the same documented shortcut ADR
 * 0057 takes for the channel, for the same reason: the alternative is a
 * register whose contents are not decoded. */
static hwaddr wifi_rx_list_head(void)
{
    uint32_t control = 0, head = 0;

    address_space_read(&address_space_memory, WDEV_CTRL_PTR,
                       MEMTXATTRS_UNSPECIFIED, &control, sizeof(control));
    if (!control) {
        return 0;
    }
    address_space_read(&address_space_memory, control, MEMTXATTRS_UNSPECIFIED,
                       &head, sizeof(head));
    return head;
}

/* How many descriptors in the chain the hardware owns. The peer is told, so it
 * can decline to spend the last one on a beacon: an association response used
 * to take it, and the second afterwards -- exactly the second the handshake's
 * first message belongs in -- had nothing free and the device never even asked
 * what was due. Beacons are the one frame here that can always wait. */
static unsigned wifi_free_count(EspWifiMac *s)
{
    hwaddr head = wifi_rx_list_head();
    hwaddr at = head ? head : s->ring;
    unsigned free = 0;

    for (unsigned i = 0; i < DESC_MAX && at; i++) {
        uint32_t desc[3] = { 0 };
        if (address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                               desc, sizeof(desc)) != MEMTX_OK) {
            break;
        }
        if (desc[0] & DESC_OWNER) {
            free++;
        }
        at = desc[2];
        if (at == head) {
            break;                      /* the chain is circular */
        }
    }
    return free;
}

static hwaddr wifi_free_descriptor(EspWifiMac *s, uint32_t *word_out)
{
    /* BOARDEMUL_WIFI_ANYDESC=1 is the behaviour this replaced, whole: start
     * from whichever descriptor the driver last handed back and take the first
     * one going that is free. Kept so the difference can be measured rather
     * than argued about. */
    hwaddr head = s->any_descriptor ? 0 : wifi_rx_list_head();
    hwaddr at = head ? head : s->ring;

    /* Only the head will do. ADR 0063 measured that and this tried to reason
     * past it: a descriptor behind the head that the hardware owns looked like
     * the write pointer, on the argument that everything before it must be a
     * frame this device had filled and so no gap could open. Wrong, and
     * measured wrong -- thirty-nine silent scans in forty-three and no
     * authentication at all, against none in fourteen with head-only. The
     * driver owns descriptors this device never filled, so a gap does open,
     * and the consumer's count runs past one and throws the frame away.
     *
     * The cost is real and is written down in ADR 0064: while the head holds
     * a frame the driver has not picked up, nothing can be delivered, however
     * many descriptors behind it are free.
     *
     * BOARDEMUL_WIFI_ANYDESC=1 restores the oldest walk, from whichever
     * descriptor the driver last handed back, which delivered out of order. */
    if (head) {
        uint32_t desc[3] = { 0 };
        if (address_space_read(&address_space_memory, head,
                               MEMTXATTRS_UNSPECIFIED, desc,
                               sizeof(desc)) != MEMTX_OK
            || !(desc[0] & DESC_OWNER)) {
            return 0;
        }
        *word_out = desc[0];
        return head;
    }

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

/* The channel the station's controller is tuned to: wDevCtrl+44, the byte
 * the ROM stamps a received frame with (ADR 0057). Compared with the access
 * point's own channel, it decides what each can hear of the other (ADR 0110). */
static unsigned wifi_channel(EspWifiMac *s)
{
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
    /* Which delivered descriptors the driver has handed back.
     *
     * This used to hold one, and ask about it at the next delivery. That
     * reads as frame loss when it is only impatience: deliveries can now
     * follow each other three hundred microseconds apart, and a driver has
     * not always re-armed the last one that soon -- it re-arms it a moment
     * later, and the count had already moved on. A run with a scan that heard
     * every beacon reported eighty-seven per cent taken, which was not true
     * of the ring and was true of the question. So the outstanding ones are
     * kept and asked about again. */
    for (unsigned i = 0; i < DESC_MAX; i++) {
        if (!s->awaiting[i]) {
            continue;
        }
        uint32_t previous[3] = { 0 };
        if (address_space_read(&address_space_memory, s->awaiting[i],
                               MEMTXATTRS_UNSPECIFIED, previous,
                               sizeof(previous)) == MEMTX_OK
            && (previous[0] & DESC_OWNER)) {
            s->taken++;
            s->awaiting[i] = 0;
        }
    }
    if (s->repeat && s->deliveries >= s->repeat) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: put %u frame(s) on; the driver "
                      "took %u%c", s->deliveries, s->taken, 10);
        wifi_raise(s, 0);
        return;
    }
    if (!s->ring) {
        /* The driver hands the ring over while it starts its station, which is
         * well after this device is created. Said once, then waited for: a
         * firmware that never starts its station retries here for the whole
         * run, and the test this used to be gated on (no deliveries yet) stays
         * true throughout, which wrote this line about 150 times a second. */
        if (!s->said_no_ring) {
            s->said_no_ring = true;
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: no receive ring handed "
                          "over yet; waiting%c", 10);
        }
        timer_mod_ns(s->hatch,
                     qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + wifi_retry_ns(s));
        return;
    }
    /* Ask the peer before any of the gates below, because they decide whether
     * a frame can go on the air and not whether there is anything to say. The
     * peer paces itself on the guest's clock and it owns the protocol, so a
     * frame it starts -- the handshake's first message -- exists only if it is
     * asked for. Gating the question meant it never was: measured, zero ticks
     * across the whole 1027 ms the station stayed associated, and the
     * handshake could not begin. ADR 0067. */
    if (!s->pending_len && !s->offered_len) {
        uint16_t flags = 0;
        s->offered_len = wifi_ask(s, FRAME_TICK, wifi_channel(s), NULL, 0,
                                  s->offered, FRAME_MAX, &flags);
        s->offered_awaited = (flags & REPLY_AWAITED) != 0;
    }
    /* The station hears the access point only while tuned to its channel
     * (ADR 0110). Nothing is exempt from this one: an answer or an awaited
     * frame cannot arrive on a channel its sender is not on. A beacon sent
     * meanwhile is missed; anything individually addressed is retried and
     * then given up, as ADR 0109 says. */
    if ((s->pending_len || s->offered_len) && wifi_channel(s) != s->ap_channel) {
        s->stalled[5]++;
        int64_t again = wifi_not_taken(s);
        timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                               + (again ? again : wifi_retry_ns(s)));
        return;
    }
    /* Nothing is heard while the MAC is held. ic_mac_deinit waits for the
     * two busy bits to drop and then sets these eight-plus-one bits;
     * ic_mac_init clears them, and the driver brackets every channel change
     * with the pair -- both read off the ROM, literals and all. A frame
     * delivered while held was the run that never heard anything: it landed
     * during bring-up, its interrupt was acknowledged unread, and with all
     * four descriptors filled and none ever re-armed the ring was dead. */
    /* Nothing is heard while the MAC is held, and that now includes answers.
     *
     * ADR 0062 exempted them, reasoning that on the air a reply is in the
     * receiver before the driver can switch it off. True, and it was the right
     * change for one authentication reply a minute. It is the wrong one for
     * ten probe answers a second: the receiver is off about three quarters of
     * the time while a station hops channels, and an exempt frame delivered
     * into that window is the very thing ADR 0057 found kills the ring -- its
     * interrupt is acknowledged unread and its descriptor is never handed
     * back. Measured: 1977 stalls on this gate against 600 deliveries, with
     * beacons crowded out and no scan hearing anything.
     *
     * What made the exemption look necessary was the retry, not the gate. An
     * answer waiting here re-arms at its own air time rather than at the
     * beacon period, so it goes out as soon as the receiver is back -- which
     * is what a real link does with a frame that arrived while the radio was
     * elsewhere: it is lost, and the station asks again. */
    /* An answer skips this gate entirely (ADR 0064, and it is load-bearing).
     * A frame the peer marked awaited skips only the receive-enable half: the
     * station is waiting for it and its receiver reads clear for most of the
     * window it is offered in, which is the same approximation the answer
     * exemption already makes. A held MAC stops both -- that is a radio tuned
     * to another channel. ADR 0068. */
    bool exempt_all = s->pending_len && s->answer_exempt;
    bool exempt_receiver = s->offered_len && s->offered_awaited;
    if (!exempt_all
        && ((s->regs[R_MAC_HOLD / 4] & MAC_HELD)
            || (!exempt_receiver
                && !(s->regs[R_RX_ENABLE / 4] & RX_ENABLED)))) {
        /* Which half closed it, counted apart, because the two mean opposite
         * things for what may be done about it. A held MAC is a radio retuned
         * to another channel, and a frame put on the air into that is one that
         * could not physically have arrived. A receiver merely switched off is
         * the ordinary case an answer is already exempted from, and the case a
         * frame the peer started has to survive somehow. */
        if (s->regs[R_MAC_HOLD / 4] & MAC_HELD) {
            s->stalled[1]++;
        } else {
            s->stalled[4]++;
        }
        /* The station's radio did not take it. An individually addressed
         * frame is retried as 802.11 retries an unacknowledged one, up to the
         * limit; a group frame is gone. ADR 0109, revisiting ADR 0067. */
        {
            int64_t again = wifi_not_taken(s);
            timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                   + (again ? again : wifi_retry_ns(s)));
        }
        return;
    }
    if (s->mark_kind && s->pending_len && s->regs[R_PROBE_GATE / 4]
        && s->pending[0] == (uint8_t)s->mark_kind) {
        int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        if (now - s->mark_at < PROBE_HOLD_MS * 1000000LL) {
            timer_mod_ns(s->hatch, now + s->tx_delay_ns);
            return;
        }
        /* Held long enough; let it go and rearm for the next one. */
        s->regs[R_PROBE_GATE / 4] = 0;
    }
    desc = wifi_free_descriptor(s, &word);
    if (!desc) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the ring at 0x%08x has no "
                      "descriptor the hardware owns after %u frame(s); waiting%c",
                      s->ring, s->deliveries, 10);
        s->stalled[2]++;
        /* The station's radio did not take it. An individually addressed
         * frame is retried as 802.11 retries an unacknowledged one, up to the
         * limit; a group frame is gone. ADR 0109, revisiting ADR 0067. */
        {
            int64_t again = wifi_not_taken(s);
            timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                   + (again ? again : wifi_retry_ns(s)));
        }
        return;
    }

    uint32_t buffer = 0;
    address_space_read(&address_space_memory, desc + 4, MEMTXATTRS_UNSPECIFIED,
                       &buffer, sizeof(buffer));
    if (!buffer) {
        /* A descriptor the hardware owns whose buffer pointer is still zero:
         * the driver has handed it back but not yet pointed it anywhere. Wait
         * -- and *re-arm*, which this did not do. A return without re-arming
         * stops the beacon schedule until something else happens to poke the
         * timer, which a transmit completion does, so the symptom was a
         * beacon rate that rose and fell with how much the station was
         * transmitting and never reached the rate that was asked for. Fifty
         * beacons a second were configured and five arrived. */
        s->stalled[3]++;
        /* The station's radio did not take it. An individually addressed
         * frame is retried as 802.11 retries an unacknowledged one, up to the
         * limit; a group frame is gone. ADR 0109, revisiting ADR 0067. */
        {
            int64_t again = wifi_not_taken(s);
            timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                   + (again ? again : wifi_retry_ns(s)));
        }
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
     * device cannot see them. The beacon now names the access point's own
     * channel, and is only delivered while the station is tuned there, so the
     * two always agree (ADR 0110). */
    /* An answer the station is waiting for goes first; a beacon is what there
     * is to say when nothing was asked. */
    size_t frame_len;
    bool answered = s->pending_len != 0;
    if (s->pending_len) {
        memcpy(frame, s->pending, s->pending_len);
        frame_len = s->pending_len;
        wifi_next_answer(s);
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms the answer, a 0x%02x "
                      "of %u byte(s), went on the air%c",
                      (unsigned)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000),
                      frame[0], (unsigned)frame_len, 10);
    } else {
        /* Whatever the peer offered on this pass, asked for above the gates so
         * that a frame it started gets asked for at all (ADR 0067). It usually
         * has a beacon; between beacons it may have the next message of a
         * handshake, and sometimes it has nothing. */
        frame_len = s->offered_len;
        memcpy(frame, s->offered, frame_len);
        s->offered_len = 0;
        s->offered_attempts = 0;
        if (!frame_len) {
            timer_mod_ns(s->hatch,
                         qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->gap_ns);
            return;
        }
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
     * to be looked at at all. Without it the driver takes every frame and no
     * scan ever reports a network.
     *
     * Which one carries it is now measured, with BOARDEMUL_WIFI_HEADER=0:...
     * one bit at a time: bit 28 alone behaves exactly like both (twenty-six
     * associations, the handshake reaching the same place), and bit 29 alone
     * gets five authentications and *no* association at all. So bit 28 is the
     * one the driver wants and bit 29 contributes nothing this firmware looks
     * at. Both are still set, because setting only what is understood would
     * be a change with no measured benefit; what is recorded is that the
     * remaining guess is bit 29 and that it is not load-bearing.
     *
     * Neither arrangement helps an EAPOL data frame reach the supplicant, so
     * this field is not what drops it (ADR 0068's open question).
     *
     * Word 0's low byte is the frame's signal strength, a signed byte in dBm
     * (`wifi_pkt_rx_ctrl_t.rssi`). Left zero, every frame arrived at 0 dBm,
     * and a station that picks its access point by signal never picks one:
     * `cnx_choose_rc` keeps the candidate whose utility beats zero, unsigned,
     * and that utility is this byte. A room's distance instead (ADR 0177). */
    header[0] = 0x30000000u | (uint8_t)RX_RSSI_DBM;
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
    /* The driver said how big the buffer is, in the descriptor's low twelve
     * bits. Writing past it would corrupt whatever the guest keeps next door,
     * which is the sort of bug that surfaces as an unrelated crash minutes
     * later -- so a frame that does not fit is dropped, loudly, the way a real
     * radio drops one it has no room for. */
    size_t room = word & DESC_SIZE_MASK;
    if (room && staged_len > room) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: a %u byte frame does not fit "
                      "the driver's %u byte buffer; dropped%c",
                      (unsigned)staged_len, (unsigned)room, 10);
        timer_mod_ns(s->hatch,
                     qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + wifi_retry_ns(s));
        return;
    }
    address_space_write(&address_space_memory, buffer, MEMTXATTRS_UNSPECIFIED,
                        staged, staged_len);
    /* Keep the size the driver gave the buffer; say how much arrived, mark it
     * the end of the frame, and hand it back by clearing the owner bit. */
    uint32_t filled = (word & DESC_SIZE_MASK)
                    | ((uint32_t)staged_len << DESC_LENGTH_SHIFT)
                    | DESC_EOF;
    address_space_write(&address_space_memory, desc, MEMTXATTRS_UNSPECIFIED,
                        &filled, sizeof(filled));
    for (unsigned i = 0; i < DESC_MAX; i++) {
        if (!s->awaiting[i]) {
            s->awaiting[i] = desc;
            break;
        }
    }
    s->deliveries++;
    if (!answered) {
        /* A frame the peer started made it into the station's receiver: tell
         * the peer on the next request, as an acknowledgement would. */
        s->offered_delivered = true;
    }
    wifi_progress(s);
    /* A data frame delivered is worth a line each, because EAPOL is the only
     * thing that is one and there are a handful per handshake -- against
     * thousands of beacons, which would drown it. Without this there was no
     * way to tell a handshake message that reached a descriptor from one the
     * closed radio dropped, which is the difference between a path bug and a
     * frame-contents bug. ADR 0067. */
    if ((frame[0] & 0x0C) == 0x08) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms a data frame of %u "
                      "byte(s) was delivered into descriptor 0x%08llx%c",
                      (unsigned)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000),
                      (unsigned)frame_len, (unsigned long long)desc, 10);
        if (s->trace_on_data && frame_len == s->trace_on_data) {
            s->trace_left = s->trace_window;
        }
    }
    if (s->deliveries <= 2) {
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: put %u byte(s) into the buffer at "
                  "0x%08x of descriptor 0x%08llx, word 0x%08x -> 0x%08x, and "
                  "raised interrupt %d%c", (unsigned)staged_len, buffer,
                  (unsigned long long)desc, word, filled, s->irq_source, 10);
    }
    /* Raised, not assigned. Assigning it wiped a transmit completion that
     * had been raised and not yet read, whenever a beacon landed in between:
     * the driver then never learned that frame had left, its queue stayed
     * busy for the rest of the run, and every send after it -- every later
     * scan's probe requests, the authentication itself -- was abandoned
     * unsent. A race with a hundred-millisecond beacon, which is why the
     * number of frames a run managed before it stuck varied from one to
     * twenty-four. */
    s->regs[R_INT_STATUS / 4] |= s->status_mask;
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
     * channel. BOARDEMUL_WIFI_GAP sets it, in milliseconds.
     *
     * Except after an answer, which comes back at once and so displaced a
     * beacon that was due. Re-arming at the gap then charged the beacon
     * schedule for the answer: a station probing eight times a second took
     * eight of the nine deliveries a second the driver's descriptors allow,
     * beacons fell to one, no scan heard anything, and the station probed
     * harder still. Measured either side of this line -- with answers
     * suppressed entirely, nine hundred beacons in a minute and every scan
     * heard one; with them, seventy-two and none did. An answer is extra to
     * the schedule, not instead of it. */
    timer_mod_ns(s->hatch, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                 + (answered && s->answer_followup
                    ? s->tx_delay_ns : s->gap_ns));
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
    qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms, after the frame, %s "
                  "+0x%03x = 0x%08x from pc 0x%08" PRIx64 "%c",
                  (unsigned)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000),
                  what, (unsigned)offset, value, pc, 10);
}

static uint64_t wifi_read(void *opaque, hwaddr offset, unsigned size)
{
    EspWifiMac *s = opaque;
    uint32_t value = offset < REG_SPAN ? s->regs[offset / 4] : 0;

    if (s->gated && offset == R_MAC_RESET) {
        value |= MAC_RESET_ACK;
    }
    /* Busy from the moment a slot is armed until its completion fires. Not
     * stored, so the driver's own hold bits in the same register are
     * untouched. */
    if (offset == R_MAC_HOLD && s->tx_pending) {
        value |= MAC_BUSY;
    }
    wifi_trace(s, "rd", offset, value);
    return value;
}

/* The frame a transmit slot was armed with, out of the queue entry. */
static size_t wifi_read_tx_frame(unsigned slot, uint8_t *out, size_t max)
{
    uint32_t buffer = 0, desc = 0, at = 0;
    uint16_t head = 0, body = 0;

    address_space_read(&address_space_memory,
                       LMAC_QUEUES + slot * LMAC_QUEUE_STRIDE,
                       MEMTXATTRS_UNSPECIFIED, &buffer, sizeof(buffer));
    if (!buffer) {
        return 0;
    }
    /* The frame is transmitted by DMA from a descriptor the eb points at, not
     * from the pointer at EB_FRAME. For a management frame the two coincide,
     * so reading EB_FRAME worked for probes, authentications and associations.
     * A data frame does not: the EAPOL reply of the four-way handshake sits
     * eighteen bytes past EB_FRAME, and reading EB_FRAME returned eighteen
     * bytes of preceding memory followed by the real frame -- which the peer
     * saw as a frame with an impossible protocol version and dropped, so the
     * handshake never advanced.
     *
     * Measured: for the reply, EB_FRAME = buffer+96 while the descriptor's
     * buffer address = buffer+114, which is exactly where the 802.11 data frame
     * (`08 01 ...`, To-DS, from the station) begins. The descriptor is a
     * standard ESP lldesc reached through EB_DESC; its second word is the
     * buffer the hardware reads. That is the address the real MAC DMAs from,
     * so it is right for both frame kinds. ADR 0069. */
    address_space_read(&address_space_memory, buffer + EB_DESC,
                       MEMTXATTRS_UNSPECIFIED, &desc, sizeof(desc));
    if (desc) {
        address_space_read(&address_space_memory, desc + LLDESC_BUF,
                           MEMTXATTRS_UNSPECIFIED, &at, sizeof(at));
    }
    if (!at) {
        /* No descriptor: fall back to the old pointer rather than lose the
         * frame. A management frame is still readable this way. */
        address_space_read(&address_space_memory, buffer + EB_FRAME,
                           MEMTXATTRS_UNSPECIFIED, &at, sizeof(at));
    }
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
/* The ethertype a data frame carries behind its LLC/SNAP header, and the EAPOL
 * key information if it is a handshake frame (0xFFFF otherwise). The header is
 * two octets longer for QoS and eight longer again behind a CCMP header. */
static void wifi_data_kind(const uint8_t *f, size_t len,
                           unsigned *ethertype, unsigned *info)
{
    size_t llc = ((f[0] & 0x80) ? 26 : 24) + ((f[1] & 0x40) ? 8 : 0);
    *ethertype = 0;
    *info = 0xFFFFu;
    if (len >= llc + 8) {
        *ethertype = ((unsigned)f[llc + 6] << 8) | f[llc + 7];
    }
    if (*ethertype == 0x888E && len >= llc + 15) {
        *info = ((unsigned)f[llc + 13] << 8) | f[llc + 14];
    }
}

static void wifi_tx_complete(void *opaque)
{
    EspWifiMac *s = opaque;

    if (!s->tx_pending) {
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms a completion fired "
                      "with nothing pending%c",
                      (unsigned)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000), 10);
        return;
    }
    uint32_t result[TX_SLOTS];
    uint8_t answer[FRAME_MAX];
    for (unsigned slot = 0; slot < TX_SLOTS; slot++) {
        result[slot] = TX_RESULT_SUCCESS;
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
        unsigned now_ms = (unsigned)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000);
        if (!length) {
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms the frame armed on "
                          "slot %u could not be read back; nothing went on the "
                          "air%c", now_ms, slot, 10);
            continue;
        }
        /* What a data frame carries, logged for every one, because the peer
         * only speaks up about the kinds it answers: a frame lost between the
         * driver and the peer, and one the peer quietly ignored, look alike
         * from its side. ADR 0095. */
        unsigned ethertype = 0, info = 0xFFFFu;
        if ((request[0] & 0x0C) == 0x08) {
            wifi_data_kind(request, length, &ethertype, &info);
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms a 0x%02x of %u "
                          "byte(s) left, ethertype 0x%04x, eapol key_info "
                          "0x%04x%c", now_ms, request[0], (unsigned)length,
                          ethertype, info, 10);
        }
        /* The access point's radio hears every frame the station sends, and
         * acknowledges the ones addressed to it; the peer says which. A group
         * frame is never acknowledged and completes as sent. With no access
         * point on the air, nothing acknowledges anything (ADR 0109). */
        bool group = length >= 10 && (request[4] & 1);
        uint16_t flags = 0;
        size_t reply = 0;
        if (s->frames_fd >= 0) {
            reply = wifi_ask(s, FRAME_TX, wifi_channel(s), request, length,
                             answer, sizeof(answer), &flags);
        } else if (!group) {
            flags = REPLY_UNACKED;
        }
        if (!group && (flags & REPLY_UNACKED)) {
            result[slot] = TX_RESULT_ACK_TIMEOUT;
        }
        if (!reply) {
            continue;
        }
        /* An earlier answer still waiting does not cost this one: it queues
         * behind it, as the access point's own transmit queue would hold it.
         * Before, the station's frame was not handed over at all, and was
         * then reported acknowledged (ADR 0109). */
        if (!s->pending_len) {
            memcpy(s->pending, answer, reply);
            s->pending_len = reply;
            s->answer_attempts = 0;
        } else if (s->backlog_count < ANSWER_BACKLOG) {
            memcpy(s->backlog[s->backlog_count], answer, reply);
            s->backlog_len[s->backlog_count++] = reply;
        } else {
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms the access point's "
                          "answers are backed up %u deep; a 0x%02x answer is "
                          "dropped%c", now_ms, ANSWER_BACKLOG + 1, answer[0], 10);
            continue;
        }
        s->answers++;
        unsigned kind = (unsigned)(request[0] >> 4) & 0xFu;
        bool fresh = !(s->kinds & (1u << kind));
        s->kinds |= (uint16_t)(1u << kind);
        if (fresh && kind != 4 && length > 24) {
            /* The body of the first frame of each kind after the probe
             * request, so what the station asked is on record: an
             * authentication's algorithm, an association's capability. */
            char body[3 * 24 + 1] = "";
            size_t n = length - 24 < 24 ? length - 24 : 24;
            for (size_t i = 0; i < n; i++) {
                snprintf(body + 3 * i, 4, "%02x ", request[24 + i]);
            }
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the 0x%02x of %u byte(s) "
                          "began %s%c", request[0], (unsigned)length, body, 10);
        }
        if (s->answers <= 8 || fresh) {
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: the station sent a 0x%02x "
                          "from %02x:%02x:%02x:%02x:%02x:%02x -- answering with "
                          "a 0x%02x of %u byte(s)%c", request[0],
                          request[10], request[11], request[12], request[13],
                          request[14], request[15], answer[0],
                          (unsigned)reply, 10);
        }
        /* Put it on soon, but not in the same instant as the
         * transmit-complete interrupt: on the air the frame leaves, and the
         * answer comes back after it. */
        timer_mod_ns(s->hatch,
                     qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + s->tx_delay_ns);
    }
    for (unsigned slot = 0; slot < TX_SLOTS; slot++) {
        if (s->tx_pending & (1u << slot)) {
            s->regs[(R_TX_RESULT0 - slot * TX_RESULT_STRIDE) / 4] = result[slot];
        }
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

    if (offset >= REG_SPAN) {
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
    if (offset == R_TX_DONE_ACK && s->frames_fd >= 0) {
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
    /* Before the modelled-slot check: say when the driver arms *any* slot in
     * the array, modelled or not. "Only slot zero has ever been seen in use"
     * was measured on management frames; a data frame may well go out on
     * another, and a slot this model does not service is a frame that is
     * never completed, never acknowledged, and never read -- which is what a
     * station retrying to its limit looks like. */
    if (s->frames_fd >= 0 && offset <= R_TX_SLOT0
        && ((R_TX_SLOT0 - offset) % 8) == 0 && (R_TX_SLOT0 - offset) / 8 < 8
        && ((uint32_t)value & TX_ARMED) == TX_ARMED
        && (s->regs[offset / 4] & TX_ARMED) != TX_ARMED) {
        unsigned any = (R_TX_SLOT0 - offset) / 8;
        if (any >= TX_SLOTS) {
            qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms the driver armed "
                          "slot %u, which this model does not service%c",
                          (unsigned)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000),
                          any, 10);
        }
    }
    int slot = s->frames_fd >= 0 ? wifi_tx_slot(offset) : -1;
    if (slot >= 0 && ((uint32_t)value & TX_ARMED) == TX_ARMED
        && (s->regs[offset / 4] & TX_ARMED) != TX_ARMED) {
        /* Armed now, by this write. Requiring the transition rather than the
         * value keeps a driver that reads the register and puts it back from
         * looking like a fresh frame. */
        s->regs[offset / 4] = (uint32_t)value;
        s->tx_pending |= 1u << slot;
        s->transmits++;
        uint8_t head[24] = { 0 };
        size_t got = wifi_read_tx_frame(slot, head, sizeof(head));
        if (s->mark_kind && got && head[0] == (uint8_t)s->mark_kind) {
            s->regs[R_PROBE_GATE / 4] = head[0];
            s->mark_at = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        }
        /* Every one, with the guest's clock beside it, so the log can be set
         * against the firmware's own timestamps. A cap here was once read as
         * a count, and a thousand lines is a price worth paying to never do
         * that again. */
        qemu_log_mask(LOG_UNIMP, "esp-wifi-mac: at %u ms the driver transmitted "
                      "on slot %d (%u so far) a 0x%02x to %02x:%02x:%02x:%02x:%02x:%02x "
                      "-- control 0x%08x, parameters 0x%08x%c",
                      (unsigned)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000),
                      slot, s->transmits, got ? head[0] : 0xFFu,
                      head[4], head[5], head[6], head[7], head[8], head[9],
                      (uint32_t)value,
                      s->regs[(R_TX_SLOT0 - 4 - slot * 8) / 4], 10);
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
    /* A reset re-associates and re-handshakes, so the old key is stale and the
     * peer will send a fresh one; drop it and go back to plaintext until then. */
    s->have_key = false;
    memset(s->tk, 0, sizeof(s->tk));
    s->offered_delivered = false;
    s->offered_unacked = false;
    s->offered_len = 0;
    s->offered_attempts = 0;
    s->answer_attempts = 0;
    s->backlog_count = 0;
    memset(s->awaiting, 0, sizeof(s->awaiting));
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
    /* The peer, if this run asked for one. Connected here rather than lazily
     * so that a socket that is not there is a complaint at start-up and not a
     * silence forty seconds into a scan. */
    s->frames_fd = -1;
    if (s->frames_path) {
        struct sockaddr_un at = { .sun_family = AF_UNIX };
        int fd = socket(AF_UNIX, SOCK_STREAM, 0);

        pstrcpy(at.sun_path, sizeof(at.sun_path), s->frames_path);
        if (fd < 0 || connect(fd, (struct sockaddr *)&at, sizeof(at)) < 0) {
            error_setg_errno(errp, errno, "esp-wifi-mac: no frame peer at %s",
                             s->frames_path);
            if (fd >= 0) {
                close(fd);
            }
            return;
        }
        s->frames_fd = fd;
    }
    s->gated = g_strcmp0(getenv("BOARDEMUL_PHY_GATES"), "0") != 0;
    s->any_descriptor = getenv("BOARDEMUL_WIFI_ANYDESC") != NULL;
    s->answer_exempt = getenv("BOARDEMUL_WIFI_GATE_ANSWERS") == NULL;
    s->answer_followup = getenv("BOARDEMUL_WIFI_NO_FOLLOWUP") == NULL;
    const char *mark = getenv("BOARDEMUL_WIFI_MARK");
    s->mark_kind = mark ? (uint32_t)strtoul(mark, NULL, 0) : 0;
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
    /* Normally the trace window opens on the first frame delivered, which is
     * after the station is up. BOARDEMUL_WIFI_TRACE_AT opens it at once
     * instead, for the questions that are about what the driver does before
     * it can hear anything -- the transmit path among them. */
    s->trace_from_start = getenv("BOARDEMUL_WIFI_TRACE_AT") != NULL;
    const char *window = getenv("BOARDEMUL_WIFI_TRACE");
    s->trace_window = window ? (unsigned)strtoul(window, NULL, 0) : 120;
    const char *on_data = getenv("BOARDEMUL_WIFI_TRACE_DATA");
    s->trace_on_data = on_data ? (unsigned)strtoul(on_data, NULL, 0) : 0;
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
                          TYPE_ESP_WIFI_MAC, REG_SPAN);
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
    /* An access point in the air: how often it beacons in milliseconds (zero
     * for none), where the peer that speaks for it listens, and the one channel
     * it is on (ADR 0110). */
    DEFINE_PROP_UINT32("beacon-ms", EspWifiMac, beacon_ms, 0),
    DEFINE_PROP_STRING("frames", EspWifiMac, frames_path),
    DEFINE_PROP_UINT32("ap-channel", EspWifiMac, ap_channel, 1),
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
