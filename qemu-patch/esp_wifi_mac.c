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
#define BEACON_MAX      96
#define SSID_MAX        32

static size_t build_beacon(uint8_t *out, const char *ssid, unsigned channel)
{
    size_t len = strlen(ssid);
    size_t at = 0;

    if (len > SSID_MAX) {
        len = SSID_MAX;
    }
    static const uint8_t head[] = {
        0x80, 0x00,                                     /* a beacon */
        0x00, 0x00,                                     /* duration */
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,             /* to everyone */
        0x02, 0x00, 0x00, 0x00, 0x00, 0x01,             /* from us */
        0x02, 0x00, 0x00, 0x00, 0x00, 0x01,             /* our network */
        0x00, 0x00,                                     /* sequence */
        0, 0, 0, 0, 0, 0, 0, 0,                         /* timestamp */
        0x64, 0x00,                                     /* every 100 */
        0x01, 0x00,                                     /* an infrastructure one */
    };
    memcpy(out, head, sizeof(head));
    at = sizeof(head);
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
    return at;
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
    /* Words of the receive header the run has been told to override, and
     * which of them. BOARDEMUL_WIFI_HEADER, as offset:value pairs. */
    uint32_t header_words[RX_HEADER / 4];
    uint32_t header_set;
    /* The channel a delivered beacon claims. Zero means "whatever the
     * controller is tuned to". BOARDEMUL_WIFI_CHANNEL, or the property. */
    uint32_t channel;
    /* The access point this device puts in the air, if any. */
    char *ssid;
    uint32_t beacon_ms;
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
    uint8_t staged[RX_HEADER + BEACON_MAX + 4] = { 0 };
    uint8_t frame[BEACON_MAX];
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
    unsigned channel = s->channel;
    if (!channel) {
        uint32_t control = 0;
        address_space_read(&address_space_memory, WDEV_CTRL_PTR,
                           MEMTXATTRS_UNSPECIFIED, &control, sizeof(control));
        uint8_t current = 0;
        if (control) {
            address_space_read(&address_space_memory, control + WDEV_CHANNEL,
                               MEMTXATTRS_UNSPECIFIED, &current, sizeof(current));
        }
        channel = current & 0x0F;
        if (channel < 1 || channel > 14) {
            channel = 1;
        }
    }
    size_t frame_len = build_beacon(frame, s->ssid ? s->ssid : "BoardEmul",
                                    channel);
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
                      "the driver took %u%c", s->deliveries, s->taken, 10);
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
    s->trace_left = s->deliveries <= 1 ? s->trace_window : 0;
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
    const char *window = getenv("BOARDEMUL_WIFI_TRACE");
    s->trace_window = window ? (unsigned)strtoul(window, NULL, 0) : 120;
    const char *mask = getenv("BOARDEMUL_WIFI_STATUS");
    s->status_mask = mask ? (uint32_t)strtoul(mask, NULL, 0) : RX_DONE_STATUS;
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
