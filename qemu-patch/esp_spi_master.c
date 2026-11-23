/*
 * SPDX-FileCopyrightText: 2026 Prolific Lab
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
/*
 * esp_spi_master -- ESP32-S3 general-purpose SPI master (SPI2/SPI3).
 *
 * SoC silicon (ADR 0011). It knows how a transfer is started and how bytes are
 * clocked out; it knows nothing about what is on the other end.
 *
 * The reason this is in C rather than Python is arithmetic. A register-level
 * bridge costs about twelve round trips per byte, and a full e-paper frame is
 * 19,200 bytes -- 232,320 round trips, roughly thirty seconds. Here the
 * registers are serviced natively and only completed transactions leave the
 * process: a frame becomes a handful of messages.
 *
 * Batching is on chip-select, which is what SPI itself defines a transaction to
 * be. The board wires chip-select and data/command to ordinary GPIOs, so which
 * pins those are arrives as properties -- configuration, not knowledge.
 *
 * Register offsets from components/soc/esp32s3/include/soc/spi_reg.h.
 */

#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "qemu/timer.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qapi/error.h"
#include "qemu/log.h"
#include "qom/object.h"
#include "hw/misc/esp_board_glue.h"

#include <sys/socket.h>
#include <sys/un.h>

OBJECT_DECLARE_SIMPLE_TYPE(EspSpiMaster, ESP_SPI_MASTER)

#define R_CMD       0x00
#define R_ADDR      0x04
#define R_CTRL      0x08
#define R_CLOCK     0x0C
#define R_USER      0x10
#define R_USER1     0x14
#define R_USER2     0x18
#define R_MS_DLEN   0x1C
#define R_MISC      0x20
#define R_W0        0x98
#define R_W15       0xD4

#define R_CLK_GATE  0xE8

#define CMD_UPDATE  (1u << 23)
#define CMD_USR     (1u << 24)

#define USER_USR_MOSI (1u << 27)
#define USER_USR_MISO (1u << 28)

/* What a transaction is made of, for how long it lasts (ADR 0120): the field
 * positions are ESP-IDF v4.4.6's soc/esp32s3/include/soc/spi_struct.h, and
 * the lengths hold their value less one. */
#define USER_DOUTDIN        (1u << 0)
#define USER_QPI_MODE       (1u << 3)
#define USER_OPI_MODE       (1u << 4)
#define USER_CS_HOLD        (1u << 6)
#define USER_CS_SETUP       (1u << 7)
#define USER_FWRITE_DUAL    (1u << 12)
#define USER_FWRITE_QUAD    (1u << 13)
#define USER_FWRITE_OCT     (1u << 14)
#define USER_USR_DUMMY      (1u << 29)
#define USER_USR_ADDR       (1u << 30)
#define USER_USR_COMMAND    (1u << 31)
#define USER1_DUMMY_CYCLELEN(v) ((v) & 0xFF)
#define USER1_CS_SETUP_TIME(v)  (((v) >> 17) & 0x1F)
#define USER1_CS_HOLD_TIME(v)   (((v) >> 22) & 0x1F)
#define USER1_ADDR_BITLEN(v)    (((v) >> 27) & 0x1F)
#define USER2_COMMAND_BITLEN(v) (((v) >> 28) & 0xF)
#define CTRL_FADDR_DUAL     (1u << 5)
#define CTRL_FADDR_QUAD     (1u << 6)
#define CTRL_FADDR_OCT      (1u << 7)
#define CTRL_FCMD_DUAL      (1u << 8)
#define CTRL_FCMD_QUAD      (1u << 9)
#define CTRL_FCMD_OCT       (1u << 10)
#define CTRL_FREAD_DUAL     (1u << 14)
#define CTRL_FREAD_QUAD     (1u << 15)
#define CTRL_FREAD_OCT      (1u << 16)
#define CLOCK_CLKCNT_N(v)   (((v) >> 12) & 0x3F)
#define CLOCK_CLKDIV_PRE(v) (((v) >> 18) & 0xF)
#define CLOCK_EQU_SYSCLK    (1u << 31)
#define CLK_GATE_MST_ACTIVE (1u << 1)
#define CLK_GATE_MST_SEL    (1u << 2)

#define SPI_REG_COUNT   (0x1000 / 4)
#define SPI_FIFO_BYTES  64
#define SPI_BUFFER_MAX  (32 * 1024)

/* One segment of a chip-select window. `dc` is the level of the data/command
 * pin while these bytes were clocked, which is the only thing that
 * distinguishes a command from payload on a three-wire display. `cs` says
 * which device on the bus was selected while they were clocked (ADR 0032);
 * it uses a byte the header already reserved. `at_ns` is the guest's time
 * when the segment is sent, so a part knows when a command happened rather
 * than when the host got to it (ADR 0112). */
typedef struct QEMU_PACKED {
    uint8_t type;      /* 0 = write only, no reply; 1 = duplex, reply expected */
    uint8_t dc;
    uint8_t cs;        /* index into the chip selects this controller drives */
    uint8_t pad;
    uint32_t len;
    uint64_t at_ns;    /* QEMU's virtual clock */
} EspSpiSegment;

#define SEGMENT_WRITE  0
#define SEGMENT_DUPLEX 1
#define SEGMENT_RELEASE 2   /* chip select went high; no bytes follow */
/* The same, for a select whose part has a BUSY line: the part answers how long
 * it is now busy (ADR 0111). */
#define SEGMENT_RELEASE_BUSY 3
/* The part's reset pin moved; `dc` carries its new level. Answered the same
 * way, and the answer replaces whatever hold was running. */
#define SEGMENT_RESET 4
/* Bytes of 0xFF this controller answered from the part's offer, which the
 * part takes now as it would have one by one (ADR 0172). No reply. */
#define SEGMENT_SKIP 5
/* The most a part may offer after a duplex answer: what it will answer to the
 * next bytes if they are all 0xFF, on the same select and data/command level.
 * emu/spi_link.py's OFFER_MAX. */
#define SPI_OFFER_MAX 2048

#define SPI_SELECTS 3

/* What the part answers: BUSY stays high `high_ns` from now. BUSY_HELD keeps
 * it high until the select next falls, and it then falls `wake_ns` after that
 * edge -- a chip asleep, woken by NSS. A `wake_ns` of BUSY_HELD means a select
 * does not wake it either: a chip held in reset.
 *
 * Then its interrupt line (ADR 0112): `line` is its level now, or LINE_LEFT
 * to leave it, and `rise_at_ns` the guest time it next rises -- a packet's
 * last bit -- or 0 for none. */
typedef struct QEMU_PACKED {
    uint32_t high_ns;
    uint32_t wake_ns;
    uint8_t line;
    uint8_t pad[3];
    uint64_t rise_at_ns;
} EspSpiBusyReply;

#define BUSY_HELD 0xFFFFFFFFu
#define LINE_LEFT 0xFF

/* A part's BUSY line, kept on the guest's clock (ADR 0111). The part decides
 * how long each hold is; this only keeps the time, which the host cannot: a
 * level it sent would arrive on its own clock, and RadioLib reads the pin a
 * microsecond after the select rises. */
typedef struct {
    int32_t pin;                /* -1: this select has no BUSY line */
    int32_t reset_pin;          /* -1: no reset line is watched */
    int32_t irq_pin;            /* -1: the part's interrupt is not ours to keep */
    QEMUTimer *timer;
    bool high;
    bool held;                  /* high until the select next falls */
    uint32_t wake_ns;           /* when held: how long after that edge */
    int64_t since;              /* when it last went high, virtual ns */
    unsigned holds;
    unsigned opened_high;       /* windows opened while high, not to wake it */
    uint64_t longest_ns;
    uint64_t total_ns;
    const char *bus;            /* for the report */
    int cs;
} EspSpiBusyLine;

struct EspSpiMaster {
    DeviceState parent_obj;

    MemoryRegion iomem;
    char *socket_path;
    char *region_name;
    uint64_t base;
    uint64_t size;
    int32_t priority;
    int32_t cs_gpio;
    int32_t cs1_gpio;
    int32_t cs2_gpio;
    int32_t dc_gpio;
    int32_t busy_gpio[SPI_SELECTS];
    int32_t reset_gpio[SPI_SELECTS];
    int32_t irq_gpio[SPI_SELECTS];
    EspSpiBusyLine busy[SPI_SELECTS];
    bool miso;
    uint32_t miso_mask;         /* which devices drive the shared MISO line */

    uint32_t regs[SPI_REG_COUNT];
    uint8_t fifo[SPI_FIFO_BYTES];

    /* Bytes accumulated since the last flush, all sharing one dc level. */
    uint8_t *buffer;
    uint32_t buffered;
    int buffered_dc;
    int buffered_cs;

    int fd;
    bool broken;

    /* The part's offer (ADR 0172): its answers to idle bytes, given ahead, on
     * one select and data/command level; how far into it the guest has
     * clocked; and how many of those the part has not yet been told about. */
    uint8_t offer[SPI_OFFER_MAX];
    uint32_t offer_len, offer_pos, offer_used;
    int offer_cs, offer_dc;

    /* SPI_USR reads as set until the transaction's bus time has passed
     * (ADR 0120). */
    int64_t usr_until;
    double clock_said[SPI_SELECTS];
};

/* Which device is selected, or -1 for none.
 *
 * Chip selects are active low, and exactly one should be asserted at a time.
 * If a board ever asserts two, the lowest index wins and the bus is
 * misconfigured anyway; saying so here would be noise on every transfer.
 */
static int spi_selected(EspSpiMaster *s)
{
    const int32_t pins[3] = { s->cs_gpio, s->cs1_gpio, s->cs2_gpio };

    for (int i = 0; i < 3; i++) {
        if (pins[i] >= 0 && !esp_gpio_pin_level(NULL, pins[i])) {
            return i;
        }
    }
    /* A controller told about no chip select at all forwards everything, which
     * is what a board with a single permanently-selected device wants. */
    if (s->cs_gpio < 0 && s->cs1_gpio < 0 && s->cs2_gpio < 0) {
        return 0;
    }
    return -1;
}

static bool spi_drives_miso(EspSpiMaster *s, int cs)
{
    return cs >= 0 && (s->miso_mask & (1u << cs)) != 0;
}

static bool spi_write_all(EspSpiMaster *s, const void *buf, size_t len)
{
    const uint8_t *p = buf;

    while (len) {
        ssize_t n = send(s->fd, p, len, MSG_NOSIGNAL);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) {
                continue;
            }
            return false;
        }
        p += n;
        len -= n;
    }
    return true;
}

static bool spi_read_all(EspSpiMaster *s, void *buf, size_t len)
{
    uint8_t *p = buf;

    while (len) {
        ssize_t n = recv(s->fd, p, len, 0);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) {
                continue;
            }
            return false;
        }
        p += n;
        len -= n;
    }
    return true;
}

static void spi_fail(EspSpiMaster *s, const char *what)
{
    if (!s->broken) {
        s->broken = true;
        error_report("esp-spi-master(%s): %s; transfers are discarded from now on",
                     s->region_name ? s->region_name : "?", what);
    }
}

/* Which select a chip-select or reset pin belongs to. A pin that is neither is
 * select zero, which is what a controller told about one device reports. */
static int spi_select_of(EspSpiMaster *s, unsigned pin)
{
    if (s->cs1_gpio >= 0 && (unsigned)s->cs1_gpio == pin) {
        return 1;
    }
    if (s->cs2_gpio >= 0 && (unsigned)s->cs2_gpio == pin) {
        return 2;
    }
    for (int i = 0; i < SPI_SELECTS; i++) {
        if (s->reset_gpio[i] >= 0 && (unsigned)s->reset_gpio[i] == pin) {
            return i;
        }
    }
    return 0;
}

/* How the line is getting on, after the 1st, 2nd, 4th, 8th hold and so on:
 * enough to see a bring-up hold by hold, never enough to flood a long run. */
static void spi_busy_report(EspSpiBusyLine *line)
{
    qemu_log_mask(LOG_UNIMP, "esp-spi-master(%s): select %d's busy line: "
                  "%u hold(s), the longest %" PRIu64 " us, %" PRIu64 " us in "
                  "all; %u window(s) opened while it was high\n",
                  line->bus, line->cs, line->holds, line->longest_ns / 1000,
                  line->total_ns / 1000, line->opened_high);
}

static void spi_busy_set(EspSpiBusyLine *line, bool high)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (high == line->high) {
        return;
    }
    line->high = high;
    if (high) {
        line->since = now;
    } else {
        uint64_t lasted = now - line->since;

        line->holds++;
        line->total_ns += lasted;
        line->longest_ns = MAX(line->longest_ns, lasted);
        if (!(line->holds & (line->holds - 1))) {
            spi_busy_report(line);
        }
    }
    esp_gpio_drive(NULL, line->pin, high);
}

static void spi_busy_fall(void *opaque)
{
    EspSpiBusyLine *line = opaque;

    line->held = false;
    spi_busy_set(line, false);
}

/* What the part answered. A release only lengthens a hold: the bytes a host
 * clocks into a waking chip do not cut its wake short. A reset edge replaces
 * it, because the reset is what decides the chip's state. */
static void spi_busy_apply(EspSpiBusyLine *line, EspSpiBusyReply reply,
                           bool replace)
{
    int64_t until;

    if (reply.high_ns == BUSY_HELD) {
        timer_del(line->timer);
        line->held = true;
        line->wake_ns = reply.wake_ns;
        spi_busy_set(line, true);
        return;
    }
    if (replace) {
        line->held = false;
        timer_del(line->timer);
        if (!reply.high_ns) {
            spi_busy_set(line, false);
            return;
        }
    } else if (!reply.high_ns || line->held) {
        return;
    }
    until = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + reply.high_ns;
    /* timer_expire_time_ns() is unsigned and says -1 for "not pending", so
     * ask whether it is pending first or every hold looks longer already. */
    if (timer_pending(line->timer)
        && (int64_t)timer_expire_time_ns(line->timer) >= until) {
        return;
    }
    timer_mod_ns(line->timer, until);
    spi_busy_set(line, true);
}

/* Send one header and wait for the part's answer about its BUSY line. A
 * controller that has lost its peer answers "not busy", which is what the
 * line did before it was modelled. */
static EspSpiBusyReply spi_ask_busy(EspSpiMaster *s, EspSpiSegment *seg)
{
    EspSpiBusyReply reply = { .line = LINE_LEFT };

    if (s->broken || s->fd < 0) {
        return reply;
    }
    if (!spi_write_all(s, seg, sizeof(*seg)) ||
        !spi_read_all(s, &reply, sizeof(reply))) {
        spi_fail(s, "busy exchange failed");
        memset(&reply, 0, sizeof(reply));
        reply.line = LINE_LEFT;
    }
    return reply;
}

/* The part's interrupt line, as it answered: a level now, and perhaps a rise
 * later, on the guest's clock (ADR 0112). A later answer replaces the rise. */
static void spi_line_apply(EspSpiBusyLine *line, EspSpiBusyReply reply)
{
    if (line->irq_pin < 0 || reply.line == LINE_LEFT) {
        return;
    }
    esp_gpio_drive(NULL, line->irq_pin, reply.line & 1);
    esp_gpio_schedule(NULL, line->irq_pin, 1,
                      reply.rise_at_ns ? (int64_t)reply.rise_at_ns : -1);
}

/* Whether a release on this select waits for the part's answer. */
static bool spi_asks(EspSpiBusyLine *line)
{
    return line->pin >= 0 || line->irq_pin >= 0;
}

/* Before anything else reaches the part, tell it how many offered bytes the
 * guest clocked, so it is where it would have been byte by byte; and drop
 * what is left of the offer, which whatever goes next may change (ADR 0172).
 * Every segment goes out through spi_flush, which starts here. */
static void spi_offer_settle(EspSpiMaster *s)
{
    EspSpiSegment seg;

    s->offer_len = s->offer_pos = 0;
    if (!s->offer_used || s->broken || s->fd < 0) {
        s->offer_used = 0;
        return;
    }
    seg.type = SEGMENT_SKIP;
    seg.dc = (uint8_t)(s->offer_dc & 1);
    seg.cs = (uint8_t)s->offer_cs;
    seg.pad = 0;
    seg.len = s->offer_used;
    seg.at_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->offer_used = 0;
    if (!spi_write_all(s, &seg, sizeof(seg))) {
        spi_fail(s, "send failed");
    }
}

/* A duplex transfer the part's offer already answers: all 0xFF, on the select
 * and level it was made for, with nothing clocked since but what it covered. */
static bool spi_offer_serves(EspSpiMaster *s, int cs, int dc, uint8_t *data,
                             uint32_t len)
{
    if (cs != s->offer_cs || dc != s->offer_dc || s->buffered
        || s->offer_pos + len > s->offer_len) {
        return false;
    }
    for (uint32_t i = 0; i < len; i++) {
        if (data[i] != 0xFF) {
            return false;
        }
    }
    memcpy(data, s->offer + s->offer_pos, len);
    s->offer_pos += len;
    s->offer_used += len;
    return true;
}

/* Hand the accumulated bytes to the model. Nothing is expected back: a
 * write-only burst is the overwhelming majority of display traffic, and not
 * waiting is what makes batching worth doing. */
static void spi_flush(EspSpiMaster *s)
{
    EspSpiSegment seg;

    spi_offer_settle(s);
    if (!s->buffered || s->broken || s->fd < 0) {
        s->buffered = 0;
        return;
    }
    seg.type = SEGMENT_WRITE;
    seg.dc = (uint8_t)(s->buffered_dc & 1);
    seg.cs = (uint8_t)(s->buffered_cs < 0 ? 0 : s->buffered_cs);
    seg.pad = 0;
    seg.len = s->buffered;
    seg.at_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!spi_write_all(s, &seg, sizeof(seg)) ||
        !spi_write_all(s, s->buffer, s->buffered)) {
        spi_fail(s, "send failed");
    }
    s->buffered = 0;
}

static void spi_append(EspSpiMaster *s, const uint8_t *data, uint32_t len)
{
    int dc = s->dc_gpio >= 0 ? esp_gpio_pin_level(NULL, s->dc_gpio) : 0;
    int cs = spi_selected(s);

    /* SPI2 is a shared bus. A device only sees bytes while its own chip select
     * is asserted, so bytes clocked out with none asserted belong to nobody. */
    if (cs < 0) {
        return;
    }

    /* A change of data/command mid-window starts a new segment, and so does a
     * change of device: buffered bytes belong to whoever was selected when
     * they were clocked. Filling the buffer also flushes, which only bounds
     * memory and is otherwise invisible to the model. */
    if (s->buffered && (dc != s->buffered_dc || cs != s->buffered_cs)) {
        spi_flush(s);
    }
    if (s->buffered + len > SPI_BUFFER_MAX) {
        spi_flush(s);
    }
    /* Written bytes may change what the part would answer next; the offer
     * no longer stands. What was used of it is still told first, at the next
     * flush, ahead of these bytes. */
    s->offer_len = s->offer_pos = 0;
    s->buffered_dc = dc;
    s->buffered_cs = cs;
    if (len > SPI_BUFFER_MAX) {
        len = SPI_BUFFER_MAX;
    }
    memcpy(s->buffer + s->buffered, data, len);
    s->buffered += len;
}

/* A transfer the guest reads back cannot be batched: it needs an answer before
 * the next instruction. Ordering is preserved by flushing first. */
static void spi_duplex(EspSpiMaster *s, uint8_t *data, uint32_t len)
{
    EspSpiSegment seg;
    int cs = spi_selected(s);
    int dc = s->dc_gpio >= 0 ? esp_gpio_pin_level(NULL, s->dc_gpio) & 1 : 0;
    uint16_t offered = 0;

    /* Idle bytes the part said ahead how it would answer: no exchange, and
     * the bus time is charged as for any other transfer (ADR 0172). */
    if (cs >= 0 && spi_offer_serves(s, cs, dc, data, len)) {
        return;
    }
    spi_flush(s);
    if (cs < 0 || s->broken || s->fd < 0) {
        memset(data, 0, len);
        return;
    }
    seg.type = SEGMENT_DUPLEX;
    seg.dc = (uint8_t)dc;
    seg.cs = (uint8_t)cs;
    seg.pad = 0;
    seg.len = len;
    seg.at_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!spi_write_all(s, &seg, sizeof(seg)) ||
        !spi_write_all(s, data, len) ||
        !spi_read_all(s, data, len) ||
        !spi_read_all(s, &offered, sizeof(offered)) ||
        offered > SPI_OFFER_MAX ||
        !spi_read_all(s, s->offer, offered)) {
        spi_fail(s, "duplex exchange failed");
        memset(data, 0, len);
        return;
    }
    s->offer_len = offered;
    s->offer_pos = 0;
    s->offer_cs = cs;
    s->offer_dc = dc;
}

/* SPI_CLK (TRM 30.7): clk_spi_mst, PLL_F80M or XTAL by SPI_MST_CLK_SEL, over
 * (CLKCNT_N + 1)(CLKDIV_PRE + 1), or undivided with CLK_EQU_SYSCLK. Until the
 * guest powers the module clock the register's own description holds: the
 * 80 MHz APB clock. */
static double spi_clock_hz(EspSpiMaster *s)
{
    uint32_t clock = s->regs[R_CLOCK / 4];
    uint32_t gate = s->regs[R_CLK_GATE / 4];
    double source = 80e6;

    if ((gate & CLK_GATE_MST_ACTIVE) && !(gate & CLK_GATE_MST_SEL)) {
        source = 40e6;
    }
    if (clock & CLOCK_EQU_SYSCLK) {
        return source;
    }
    return source / ((CLOCK_CLKCNT_N(clock) + 1.0)
                     * (CLOCK_CLKDIV_PRE(clock) + 1.0));
}

static unsigned spi_lines(uint32_t user, bool dual, bool quad, bool oct)
{
    if (oct || (user & USER_OPI_MODE)) {
        return 8;
    }
    if (quad || (user & USER_QPI_MODE)) {
        return 4;
    }
    return dual ? 2 : 1;
}

/* The SPI_CLK cycles a transaction takes, state by state (TRM 30.5.8.1). */
static uint64_t spi_cycles(EspSpiMaster *s)
{
    uint32_t user = s->regs[R_USER / 4];
    uint32_t user1 = s->regs[R_USER1 / 4];
    uint32_t user2 = s->regs[R_USER2 / 4];
    uint32_t ctrl = s->regs[R_CTRL / 4];
    uint32_t bits = (s->regs[R_MS_DLEN / 4] & 0x3FFFF) + 1;
    uint64_t cycles = 0;

    if (user & USER_CS_SETUP) {
        cycles += USER1_CS_SETUP_TIME(user1) + 1;
    }
    if (user & USER_USR_COMMAND) {
        cycles += DIV_ROUND_UP(USER2_COMMAND_BITLEN(user2) + 1,
                               spi_lines(user, ctrl & CTRL_FCMD_DUAL,
                                         ctrl & CTRL_FCMD_QUAD,
                                         ctrl & CTRL_FCMD_OCT));
    }
    if (user & USER_USR_ADDR) {
        cycles += DIV_ROUND_UP(USER1_ADDR_BITLEN(user1) + 1,
                               spi_lines(user, ctrl & CTRL_FADDR_DUAL,
                                         ctrl & CTRL_FADDR_QUAD,
                                         ctrl & CTRL_FADDR_OCT));
    }
    if (user & USER_USR_DUMMY) {
        cycles += USER1_DUMMY_CYCLELEN(user1) + 1;
    }
    uint64_t out = DIV_ROUND_UP(bits, spi_lines(user, user & USER_FWRITE_DUAL,
                                                user & USER_FWRITE_QUAD,
                                                user & USER_FWRITE_OCT));
    uint64_t in = DIV_ROUND_UP(bits, spi_lines(user, ctrl & CTRL_FREAD_DUAL,
                                               ctrl & CTRL_FREAD_QUAD,
                                               ctrl & CTRL_FREAD_OCT));
    bool writes = user & USER_USR_MOSI;
    bool reads = user & USER_USR_MISO;
    if (writes && reads && (user & USER_DOUTDIN)) {
        cycles += MAX(out, in);         /* full duplex: one data phase */
    } else {
        cycles += (writes ? out : 0) + (reads ? in : 0);
    }
    if (user & USER_CS_HOLD) {
        cycles += USER1_CS_HOLD_TIME(user1) + 1;
    }
    return cycles;
}

/* Whether a transaction is still on the bus. Asked at the moment the guest
 * looks, on its own clock, so the answer changes exactly when the last bit
 * goes: a timer would fire whenever the host got round to it. */
static bool spi_on_bus(EspSpiMaster *s)
{
    return (s->regs[R_CMD / 4] & CMD_USR)
           && qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) < s->usr_until;
}

/* Hold SPI_USR set for the transaction just begun. One begun while another
 * is still on the bus follows it. */
static void spi_occupy(EspSpiMaster *s)
{
    double hz = spi_clock_hz(s);
    int cs = spi_selected(s);
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (cs >= 0 && cs < SPI_SELECTS && s->clock_said[cs] != hz) {
        s->clock_said[cs] = hz;
        qemu_log_mask(LOG_UNIMP, "esp-spi-master(%s): select %d clocked at "
                      "%.0f Hz\n", s->region_name ? s->region_name : "?",
                      cs, hz);
    }
    s->usr_until = MAX(now, s->usr_until)
                   + (int64_t)(spi_cycles(s) * 1e9 / hz);
    s->regs[R_CMD / 4] |= CMD_USR;
}

static void spi_start(EspSpiMaster *s)
{
    uint32_t user = s->regs[R_USER / 4];
    uint32_t bits = (s->regs[R_MS_DLEN / 4] & 0x3FFFF) + 1;
    uint32_t bytes = (bits + 7) / 8;

    if (bytes > SPI_FIFO_BYTES) {
        bytes = SPI_FIFO_BYTES;
    }
    /* Arduino's SPI.transfer() returns the received byte, so it sets USR_MISO
     * on every transfer whether or not anything is wired to it. Honouring that
     * blindly means a synchronous round trip per byte and no batching at all.
     * Where the board has no MISO line -- an e-paper panel drives nothing back
     * -- the answer is known to be zero and can be given here. */
    if ((user & USER_USR_MISO) && spi_drives_miso(s, spi_selected(s))) {
        spi_duplex(s, s->fifo, bytes);
        return;
    }
    if (user & (USER_USR_MOSI | USER_USR_MISO)) {
        spi_append(s, s->fifo, bytes);
    }
    if (user & USER_USR_MISO) {
        memset(s->fifo, 0, bytes);
    }
}

static void spi_cs_changed(void *opaque, unsigned pin, int level)
{
    EspSpiMaster *s = opaque;
    EspSpiSegment seg;
    /* Which device's select moved. The level has already changed, so ask the
     * pin rather than spi_selected(), which would answer -1 on a release. */
    int cs = spi_select_of(s, pin);
    EspSpiBusyLine *line = &s->busy[cs];

    /* Chip-select is active low. A falling edge opens a window: it wakes a
     * part that is asleep, and a host that opens one while BUSY is otherwise
     * high has not waited for it. */
    if (!level) {
        if (line->pin < 0) {
            return;
        }
        if (line->held) {
            if (line->wake_ns != BUSY_HELD) {
                line->held = false;
                timer_mod_ns(line->timer,
                             qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + line->wake_ns);
            }
        } else if (line->high) {
            line->opened_high++;
        }
        return;
    }

    /* A rising edge ends the transaction. */
    spi_flush(s);

    /* A device whose commands vary in length needs the end of the window; one
     * that does not simply ignores this. */
    if (s->broken || s->fd < 0) {
        return;
    }
    seg.type = spi_asks(line) ? SEGMENT_RELEASE_BUSY : SEGMENT_RELEASE;
    seg.dc = 0;
    seg.cs = (uint8_t)cs;
    seg.pad = 0;
    seg.len = 0;
    seg.at_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (spi_asks(line)) {
        EspSpiBusyReply reply = spi_ask_busy(s, &seg);

        if (line->pin >= 0) {
            spi_busy_apply(line, reply, false);
        }
        spi_line_apply(line, reply);
        return;
    }
    if (!spi_write_all(s, &seg, sizeof(seg))) {
        spi_fail(s, "send failed");
    }
}

/* A part's reset pin moved. The part is told, since a reset is what returns
 * it to its reset state, and says how long it is busy coming out of it. */
static void spi_reset_changed(void *opaque, unsigned pin, int level)
{
    EspSpiMaster *s = opaque;
    int cs = spi_select_of(s, pin);
    EspSpiSegment seg = {
        .type = SEGMENT_RESET, .dc = (uint8_t)(level & 1),
        .cs = (uint8_t)cs, .pad = 0, .len = 0,
    };
    EspSpiBusyReply reply;

    spi_flush(s);
    seg.at_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    reply = spi_ask_busy(s, &seg);
    if (s->busy[cs].pin >= 0) {
        spi_busy_apply(&s->busy[cs], reply, true);
    }
    spi_line_apply(&s->busy[cs], reply);
}

static uint64_t spi_read(void *opaque, hwaddr offset, unsigned size)
{
    EspSpiMaster *s = opaque;

    if (offset >= R_W0 && offset <= R_W15 + 3) {
        uint32_t i = offset - R_W0;
        uint32_t word = ldl_le_p(s->fifo + (i & ~3u));
        /* Investigation hatch: what the guest actually reads back. */
        if (getenv("BOARDEMUL_SPI_FIFO_TRACE")) {
            qemu_log_mask(LOG_UNIMP, "FIFOREAD %s +0x%02x size=%u = 0x%08x\n",
                          s->region_name ? s->region_name : "?", i, size, word);
        }
        return word;
    }
    if (offset / 4 >= SPI_REG_COUNT) {
        return 0;
    }
    if (offset == R_CMD && (s->regs[R_CMD / 4] & CMD_USR) && !spi_on_bus(s)) {
        s->regs[R_CMD / 4] &= ~CMD_USR;
    }
    return s->regs[offset / 4];
}

static void spi_write(void *opaque, hwaddr offset, uint64_t val, unsigned size)
{
    EspSpiMaster *s = opaque;
    uint32_t value = (uint32_t)val;

    if (offset >= R_W0 && offset <= R_W15 + 3) {
        stl_le_p(s->fifo + ((offset - R_W0) & ~3u), value);
        return;
    }
    if (offset / 4 >= SPI_REG_COUNT) {
        return;
    }
    if (offset == R_CMD) {
        /* USR and UPDATE are self-clearing triggers. Storing them verbatim
         * makes the driver's own read-modify-write re-fire the transfer. USR
         * reads as set while a transaction is on the bus (ADR 0120). */
        bool busy = spi_on_bus(s);
        s->regs[R_CMD / 4] = (value & ~(CMD_USR | CMD_UPDATE))
                             | (busy ? CMD_USR : 0);
        if (value & CMD_USR) {
            spi_start(s);
            spi_occupy(s);
        }
        return;
    }
    s->regs[offset / 4] = value;
}

static const MemoryRegionOps spi_ops = {
    .read = spi_read,
    .write = spi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

static void spi_realize(DeviceState *dev, Error **errp)
{
    EspSpiMaster *s = ESP_SPI_MASTER(dev);
    struct sockaddr_un addr;

    if (!s->socket_path) {
        error_setg(errp, "esp-spi-master: 'socket' property is required");
        return;
    }
    if (strlen(s->socket_path) >= sizeof(addr.sun_path)) {
        error_setg(errp, "esp-spi-master: socket path too long");
        return;
    }
    s->fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (s->fd < 0) {
        error_setg_errno(errp, errno, "esp-spi-master: socket()");
        return;
    }
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    pstrcpy(addr.sun_path, sizeof(addr.sun_path), s->socket_path);
    if (connect(s->fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        error_setg_errno(errp, errno, "esp-spi-master: connect(%s)",
                         s->socket_path);
        close(s->fd);
        s->fd = -1;
        return;
    }

    s->buffer = g_malloc(SPI_BUFFER_MAX);
    /* Legacy spelling: a bus-wide `miso` switch is device 0 driving the line. */
    if (s->miso) {
        s->miso_mask |= 1u;
    }
    if (s->cs_gpio >= 0) {
        esp_gpio_watch(NULL, s->cs_gpio, spi_cs_changed, s);
    }
    if (s->cs1_gpio >= 0) {
        esp_gpio_watch(NULL, s->cs1_gpio, spi_cs_changed, s);
    }
    if (s->cs2_gpio >= 0) {
        esp_gpio_watch(NULL, s->cs2_gpio, spi_cs_changed, s);
    }
    for (int i = 0; i < SPI_SELECTS; i++) {
        EspSpiBusyLine *line = &s->busy[i];

        line->pin = s->busy_gpio[i];
        line->reset_pin = s->reset_gpio[i];
        line->irq_pin = s->irq_gpio[i];
        line->bus = s->region_name ? s->region_name : "?";
        line->cs = i;
        if (line->pin >= 0) {
            line->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, spi_busy_fall, line);
        }
        if (line->reset_pin >= 0) {
            esp_gpio_watch(NULL, line->reset_pin, spi_reset_changed, s);
        }
    }

    memory_region_init_io(&s->iomem, OBJECT(s), &spi_ops, s,
                          s->region_name ? s->region_name : TYPE_ESP_SPI_MASTER,
                          s->size);
    memory_region_add_subregion_overlap(get_system_memory(), s->base,
                                        &s->iomem, s->priority);
}

static Property spi_props[] = {
    DEFINE_PROP_STRING("socket", EspSpiMaster, socket_path),
    DEFINE_PROP_STRING("name", EspSpiMaster, region_name),
    DEFINE_PROP_UINT64("base", EspSpiMaster, base, 0x60024000),
    DEFINE_PROP_UINT64("size", EspSpiMaster, size, 0x1000),
    DEFINE_PROP_INT32("priority", EspSpiMaster, priority, 100),
    DEFINE_PROP_INT32("cs_gpio", EspSpiMaster, cs_gpio, -1),
    DEFINE_PROP_INT32("cs1_gpio", EspSpiMaster, cs1_gpio, -1),
    DEFINE_PROP_INT32("cs2_gpio", EspSpiMaster, cs2_gpio, -1),
    DEFINE_PROP_INT32("dc_gpio", EspSpiMaster, dc_gpio, -1),
    /* A part's BUSY and reset pins, per select (ADR 0111). Wiring, like the
     * selects: what the part does with them is the part's to say. */
    DEFINE_PROP_INT32("busy_gpio", EspSpiMaster, busy_gpio[0], -1),
    DEFINE_PROP_INT32("busy1_gpio", EspSpiMaster, busy_gpio[1], -1),
    DEFINE_PROP_INT32("busy2_gpio", EspSpiMaster, busy_gpio[2], -1),
    DEFINE_PROP_INT32("reset_gpio", EspSpiMaster, reset_gpio[0], -1),
    DEFINE_PROP_INT32("reset1_gpio", EspSpiMaster, reset_gpio[1], -1),
    DEFINE_PROP_INT32("reset2_gpio", EspSpiMaster, reset_gpio[2], -1),
    /* A part's interrupt line, per select (ADR 0112): driven as the part's
     * answers say, on the guest's clock. */
    DEFINE_PROP_INT32("irq_gpio", EspSpiMaster, irq_gpio[0], -1),
    DEFINE_PROP_INT32("irq1_gpio", EspSpiMaster, irq_gpio[1], -1),
    DEFINE_PROP_INT32("irq2_gpio", EspSpiMaster, irq_gpio[2], -1),
    DEFINE_PROP_BOOL("miso", EspSpiMaster, miso, true),
    /* One bit per chip-select index: whether that part drives MISO. */
    DEFINE_PROP_UINT32("miso_mask", EspSpiMaster, miso_mask, 0),
    DEFINE_PROP_END_OF_LIST(),
};

static void spi_init(Object *obj)
{
    EspSpiMaster *s = ESP_SPI_MASTER(obj);

    s->fd = -1;
    s->buffered_dc = -1;
}

static void spi_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = spi_realize;
    dc->user_creatable = true;
    device_class_set_props(dc, spi_props);
}

static const TypeInfo spi_info = {
    .name = TYPE_ESP_SPI_MASTER,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspSpiMaster),
    .instance_init = spi_init,
    .class_init = spi_class_init,
};

static void spi_register_types(void)
{
    type_register_static(&spi_info);
}

type_init(spi_register_types)
