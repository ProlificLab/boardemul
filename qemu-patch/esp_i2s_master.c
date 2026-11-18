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
 * then hands the DMA engine a buffer whenever the next descriptor in the ring
 * belongs to the hardware: silence, or a WAV file given as the microphone
 * (ADR 0146). What a transmit stream plays can be written to a WAV file. The
 * transfer takes as long as the sample clock the guest set says it does, in
 * both directions (ADR 0119).
 *
 * Every register number and bit position here comes from Espressif's headers
 * and the ESP32-S3 technical reference manual. An earlier attempt inferred them
 * from bus traffic instead, got the direction wrong, and drained a transmit
 * channel that had never started -- clearing descriptor owner bits in whatever
 * guest memory the stale link happened to point at. ADR 0015 records both that
 * and what this model does and does not achieve.
 */

#include "qemu/osdep.h"
#include "qemu/bitops.h"
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
#include "hw/core/cpu.h"
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

/* GDMA_IN_CONF1's CHECK_OWNER, bit 12: whether the channel heeds a
 * descriptor's owner bit at all. */
#define GDMA_CHECK_OWNER (1u << 12)

/* GDMA_PERI_IN_SEL is six bits. */
#define PERI_SEL_MASK   0x3F

/* Descriptors live in the lower part of DRAM, which is why the link register
 * only carries twenty bits of address. */
#define DRAM_TOP_BITS   ((ESP_GDMA_RAM_ADDR >> 20) << 20)

/* How often an idle block looks for a stream to start, and how a stream whose
 * rate cannot be known is paced (ADR 0119): fast enough that a driver waiting
 * on a buffer is not visibly delayed, and nothing while no stream runs. */
#define I2S_POLL_NS (5 * 1000 * 1000)

/* Where each direction's clock and frame are set (i2s_reg.h). Receive and
 * transmit have the same fields at their own offsets. */
typedef struct I2sDirection {
    const char *name;
    hwaddr conf, conf1, clkm_conf, clkm_div_conf, tdm_ctrl;
} I2sDirection;

static const I2sDirection I2S_RX = { "receive", 0x20, 0x28, 0x30, 0x38, 0x50 };
static const I2sDirection I2S_TX = { "transmit", 0x24, 0x2C, 0x34, 0x3C, 0x54 };

#define CONF_SLAVE_MOD          (1u << 3)
#define CONF_UPDATE             (1u << 8)
#define CONF_24_FILL_EN         (1u << 16)
#define CONF_TDM_EN             (1u << 19)
#define CONF_PDM_EN             (1u << 20)
#define CONF1_BCK_DIV_NUM(v)    (((v) >> 7) & 0x3F)
#define CONF1_BITS_MOD(v)       (((v) >> 13) & 0x1F)
#define CONF1_HALF_SAMPLE(v)    (((v) >> 18) & 0x3F)
#define CLKM_DIV_NUM(v)         ((v) & 0xFF)
#define CLKM_ACTIVE             (1u << 26)
#define CLKM_SEL(v)             (((v) >> 27) & 0x3)
#define DIV_Z(v)                ((v) & 0x1FF)
#define DIV_Y(v)                (((v) >> 9) & 0x1FF)
#define DIV_X(v)                (((v) >> 18) & 0x1FF)
#define DIV_YN1(v)              (((v) >> 27) & 0x1)
#define TDM_CHAN_EN(v)          ((v) & 0xFFFF)
#define TDM_TOT_CHAN_NUM(v)     (((v) >> 16) & 0xF)

/* TRM 28.6: CLK_SEL 0 is XTAL_CLK, 1 PLL_D2_CLK, 2 PLL_F160M_CLK; 3 is the
 * MCLK input, whose rate nothing here knows. */
static const double I2S_SOURCE_HZ[3] = { 40e6, 240e6, 160e6 };

/* Descriptor: size 0..11, length 12..23, suc_eof at 30, owner at 31. */
#define DESC_BYTES 12
#define DESC_MAX_SIZE 4092

/* Bounds on a transmit list; beyond these it is not a list. */
#define TX_MAX_DESCRIPTORS 64
#define TX_MAX_TRANSFER (256 * 1024)

/* What a stream carries, as its registers say (ADR 0146): whole hertz,
 * enabled slots, and each slot's width in bits and in bytes. */
typedef struct I2sFormat {
    unsigned rate_hz;
    unsigned channels;
    unsigned bits;
    unsigned bytes;
} I2sFormat;

/* A WAV header is 44 bytes in the plain PCM form written here. */
#define WAV_HEADER 44

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
    /* When the buffer each direction is clocking began, or 0 while none is
     * (ADR 0119); and whether this stream's rate has been reported. */
    int64_t rx_start;
    int64_t tx_start;
    int64_t tx_end;             /* when the last transmit list finished */
    bool rx_said;
    bool tx_said;
    uint64_t buffers;
    uint64_t bytes;
    bool complained;
    const char *idle_reason;
    const char *rx_idle_reason;

    /* ADR 0146: where played streams are written, and the file being
     * written; a WAV file the microphone hears, read once at realize. */
    char *speaker;
    char *mic;
    I2sFormat tx_fmt;
    I2sFormat rx_fmt;
    FILE *wav;
    uint32_t wav_bytes;
    unsigned wav_count;
    bool wav_refused;
    int32_t *mic_data;          /* left-justified samples, frame by frame */
    uint32_t mic_frames;
    unsigned mic_channels;
    unsigned mic_rate;
    uint64_t rx_frames;         /* frames this receive stream has heard */
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
 * hardware does and what stops a buffer being handled twice -- when the
 * channel asks it to. GDMA_IN_CONF1's CHECK_OWNER says whether it does, and a
 * channel that has not set it fills each buffer whatever its owner bit says
 * (TRM, GDMA; ADR 0146). ESP-IDF 4.4's I2S driver leaves it clear.
 *
 * Receiving, the interesting number is `size`, the room available. Transmitting
 * it is `length`, the bytes the driver actually put there.
 *
 * `suc_eof` means different things by direction, which matters a great deal.
 * Transmitting, it marks the last buffer of the list and the transfer ends
 * there. Receiving, the engine's own comment is that it only raises a status
 * bit and does not stop anything, because the ring keeps turning.
 */
static bool i2s_descriptor(uint32_t at, bool receiving, bool check_owner,
                           uint32_t *bytes, uint32_t *next, bool *eof)
{
    uint8_t raw[DESC_BYTES];

    if (address_space_read(&address_space_memory, at, MEMTXATTRS_UNSPECIFIED,
                           raw, sizeof(raw)) != MEMTX_OK) {
        return false;
    }
    uint32_t config = ldl_le_p(raw);
    uint32_t want = receiving ? (config & 0xFFF) : ((config >> 12) & 0xFFF);
    bool owner = (config >> 31) & 1;

    if ((check_owner && !owner) || want == 0 || want > DESC_MAX_SIZE) {
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

/* The bytes a second a direction moves through memory, from its registers
 * (TRM 28.6 and 28.9; ADR 0119), or 0 with the reason it cannot be known.
 * The counting fields hold their value less one, as ESP-IDF's i2s_ll.h
 * writes them. */
static double i2s_byte_rate(EspI2sMaster *s, const I2sDirection *d,
                            const char **why, char *how, size_t how_len,
                            I2sFormat *fmt)
{
    uint32_t conf = s->regs[d->conf / 4];
    uint32_t conf1 = s->regs[d->conf1 / 4];
    uint32_t clkm = s->regs[d->clkm_conf / 4];
    uint32_t div = s->regs[d->clkm_div_conf / 4];
    uint32_t tdm = s->regs[d->tdm_ctrl / 4];

    if (conf & CONF_SLAVE_MOD) {
        *why = "slave mode: BCK comes from the codec, which is not modelled";
        return 0;
    }
    if ((conf & CONF_PDM_EN) || !(conf & CONF_TDM_EN)) {
        *why = "not TDM mode";
        return 0;
    }
    if (!(clkm & CLKM_ACTIVE)) {
        *why = "its clock is not active";
        return 0;
    }
    if (CLKM_SEL(clkm) == 3) {
        *why = "its clock is the MCLK input";
        return 0;
    }

    /* N + b/a: N is CLKM_DIV_NUM, but 0 means 256 and 1 means 2; a and b
     * come back out of x, y, z and yn1 by the TRM's formulas, and z of 0 is
     * the integer divider. */
    unsigned n = CLKM_DIV_NUM(clkm);
    double divider = n == 0 ? 256 : n == 1 ? 2 : n;
    unsigned z = DIV_Z(div);
    if (z) {
        double a = (double)(DIV_X(div) + 1) * z + DIV_Y(div);
        double b = DIV_YN1(div) ? a - z : z;
        divider += b / a;
    }
    double bck = I2S_SOURCE_HZ[CLKM_SEL(clkm)] / divider
                 / (CONF1_BCK_DIV_NUM(conf1) + 1);
    double ws = bck / (2.0 * (CONF1_HALF_SAMPLE(conf1) + 1));

    unsigned bits = CONF1_BITS_MOD(conf1) + 1;
    unsigned bytes = (bits == 24 && (conf & CONF_24_FILL_EN)) ? 4
                     : (bits + 7) / 8;
    uint32_t slots = (2u << TDM_TOT_CHAN_NUM(tdm)) - 1;
    unsigned channels = ctpop32(TDM_CHAN_EN(tdm) & slots);
    if (!channels) {
        *why = "no channel is enabled";
        return 0;
    }
    snprintf(how, how_len, "%.0f Hz, %u channel(s) of %u bit(s)",
             ws, channels, bits);
    fmt->rate_hz = (unsigned)(ws + 0.5);
    fmt->channels = channels;
    fmt->bits = bits;
    fmt->bytes = bytes;
    return ws * channels * bytes;
}

/* The rate for a direction, saying once per stream what it is or why it is
 * not known. */
static double i2s_rate(EspI2sMaster *s, const I2sDirection *d, bool *said,
                       I2sFormat *fmt)
{
    const char *why = NULL;
    char how[64] = "";
    double rate = i2s_byte_rate(s, d, &why, how, sizeof(how), fmt);

    if (!*said) {
        *said = true;
        if (rate > 0) {
            qemu_log_mask(LOG_UNIMP, "esp-i2s-master(%s): %s at %s, %.0f "
                          "bytes a second\n", s->name ? s->name : "i2s",
                          d->name, how, rate);
        } else {
            qemu_log_mask(LOG_UNIMP, "esp-i2s-master(%s): %s paced every "
                          "%d ms: %s\n", s->name ? s->name : "i2s", d->name,
                          I2S_POLL_NS / 1000000, why);
        }
    }
    return rate;
}

static int64_t i2s_clocked_ns(uint32_t bytes, double rate)
{
    return (int64_t)(bytes * 1e9 / rate);
}

static void i2s_rx_idle(EspI2sMaster *s, const char *reason, uint32_t chan);

/* -- ADR 0146: sound out and in ---------------------------------------- */

/* Write the header for `data` bytes of this format at the file's start, and
 * go back to its end: the file is a valid WAV after every buffer. */
static void i2s_wav_header(FILE *f, const I2sFormat *fmt, uint32_t data)
{
    uint8_t h[WAV_HEADER];
    uint32_t block = fmt->channels * fmt->bytes;

    memcpy(h, "RIFF", 4);
    stl_le_p(h + 4, 36 + data);
    memcpy(h + 8, "WAVEfmt ", 8);
    stl_le_p(h + 16, 16);
    stw_le_p(h + 20, 1);                        /* PCM */
    stw_le_p(h + 22, fmt->channels);
    stl_le_p(h + 24, fmt->rate_hz);
    stl_le_p(h + 28, fmt->rate_hz * block);
    stw_le_p(h + 32, block);
    stw_le_p(h + 34, fmt->bytes * 8);
    memcpy(h + 36, "data", 4);
    stl_le_p(h + 40, data);
    if (fseek(f, 0, SEEK_SET) == 0) {
        fwrite(h, 1, sizeof(h), f);
    }
    fseek(f, 0, SEEK_END);
}

/* A played buffer, onto the end of this stream's file; the first one of a
 * stream opens the next file. 8-bit WAV is unsigned where I2S is signed. */
static void i2s_speaker_write(EspI2sMaster *s, uint8_t *played, uint32_t total)
{
    if (!s->speaker || !s->speaker[0] || s->wav_refused) {
        return;
    }
    if (!s->wav) {
        g_autofree char *path = g_strdup_printf("%s-%u.wav", s->speaker,
                                                ++s->wav_count);
        s->wav = fopen(path, "wb+");
        if (!s->wav) {
            warn_report("%s: cannot write %s: %s", s->name ? s->name : "i2s",
                        path, strerror(errno));
            s->wav_refused = true;
            return;
        }
        s->wav_bytes = 0;
        i2s_wav_header(s->wav, &s->tx_fmt, 0);
        info_report("%s: what it plays goes to %s (%u Hz, %u channel(s) of "
                    "%u bit(s))", s->name ? s->name : "i2s", path,
                    s->tx_fmt.rate_hz, s->tx_fmt.channels, s->tx_fmt.bytes * 8);
    }
    if (s->tx_fmt.bytes == 1) {
        for (uint32_t i = 0; i < total; i++) {
            played[i] ^= 0x80;
        }
    }
    if (fwrite(played, 1, total, s->wav) == total) {
        s->wav_bytes += total;
        i2s_wav_header(s->wav, &s->tx_fmt, s->wav_bytes);
        fflush(s->wav);
    }
}

static void i2s_speaker_close(EspI2sMaster *s)
{
    if (s->wav) {
        fclose(s->wav);
        s->wav = NULL;
    }
}

/* Read a PCM WAV file into left-justified 32-bit samples. */
static bool i2s_mic_load(EspI2sMaster *s, Error **errp)
{
    g_autofree gchar *data = NULL;
    gsize n = 0;
    GError *err = NULL;
    const uint8_t *d;
    const uint8_t *samples = NULL;
    uint32_t sample_bytes = 0;
    unsigned format = 0, channels = 0, rate = 0, bits = 0;

    if (!g_file_get_contents(s->mic, &data, &n, &err)) {
        error_setg(errp, "esp-i2s-master: cannot read mic %s: %s", s->mic,
                   err->message);
        g_error_free(err);
        return false;
    }
    d = (const uint8_t *)data;
    if (n < 12 || memcmp(d, "RIFF", 4) || memcmp(d + 8, "WAVE", 4)) {
        error_setg(errp, "esp-i2s-master: %s is not a WAV file", s->mic);
        return false;
    }
    for (gsize at = 12; at + 8 <= n;) {
        uint32_t size = ldl_le_p(d + at + 4);
        const uint8_t *body = d + at + 8;
        if (size > n - at - 8) {
            size = n - at - 8;
        }
        if (!memcmp(d + at, "fmt ", 4) && size >= 16) {
            format = lduw_le_p(body);
            channels = lduw_le_p(body + 2);
            rate = ldl_le_p(body + 4);
            bits = lduw_le_p(body + 14);
            if (format == 0xFFFE && size >= 26) {
                format = lduw_le_p(body + 24);  /* the sub-format's first word */
            }
        } else if (!memcmp(d + at, "data", 4)) {
            samples = body;
            sample_bytes = size;
        }
        at += 8 + size + (size & 1);
    }
    if (format != 1 || !channels || !rate || !samples ||
        (bits != 8 && bits != 16 && bits != 24 && bits != 32)) {
        error_setg(errp, "esp-i2s-master: %s is not PCM of 8, 16, 24 or 32 "
                   "bits (format %u, %u bit(s))", s->mic, format, bits);
        return false;
    }

    unsigned width = bits / 8;
    s->mic_channels = channels;
    s->mic_rate = rate;
    s->mic_frames = sample_bytes / (width * channels);
    s->mic_data = g_new0(int32_t, (size_t)s->mic_frames * channels);
    for (uint32_t i = 0; i < s->mic_frames * channels; i++) {
        const uint8_t *p = samples + (size_t)i * width;
        int32_t v;
        switch (width) {
        case 1:
            v = (int32_t)((uint32_t)(p[0] ^ 0x80) << 24);
            break;
        case 2:
            v = (int32_t)((uint32_t)lduw_le_p(p) << 16);
            break;
        case 3:
            v = (int32_t)(((uint32_t)p[0] << 8) | ((uint32_t)p[1] << 16) |
                          ((uint32_t)p[2] << 24));
            break;
        default:
            v = (int32_t)ldl_le_p(p);
            break;
        }
        s->mic_data[i] = v;
    }
    info_report("esp-i2s-master(%s): the microphone hears %s, %u Hz, %u "
                "channel(s), %.1f s", s->name ? s->name : "i2s", s->mic, rate,
                channels, (double)s->mic_frames / rate);
    return true;
}

/* Fill a receive buffer from the microphone's file, converted to the
 * stream's format: the nearest earlier sample, slots repeating the file's
 * channels, and each slot's width (ADR 0146). Past the file's end, silence. */
static void i2s_mic_fill(EspI2sMaster *s, uint8_t *buf, uint32_t bytes)
{
    const I2sFormat *f = &s->rx_fmt;
    uint32_t frame_bytes = f->channels * f->bytes;

    if (!s->mic_data || !frame_bytes || !f->rate_hz) {
        return;
    }
    for (uint32_t at = 0; at + frame_bytes <= bytes; at += frame_bytes) {
        uint64_t src = s->rx_frames * s->mic_rate / f->rate_hz;
        s->rx_frames++;
        for (unsigned c = 0; c < f->channels; c++) {
            int32_t v = src < s->mic_frames
                ? s->mic_data[src * s->mic_channels + c % s->mic_channels] : 0;
            uint8_t *p = buf + at + c * f->bytes;
            switch (f->bytes) {
            case 1:
                p[0] = (uint8_t)((uint32_t)v >> 24);
                break;
            case 2:
                stw_le_p(p, (uint16_t)((uint32_t)v >> 16));
                break;
            case 3:
                p[0] = (uint8_t)((uint32_t)v >> 8);
                p[1] = (uint8_t)((uint32_t)v >> 16);
                p[2] = (uint8_t)((uint32_t)v >> 24);
                break;
            default:
                stl_le_p(p, (uint32_t)v);
                break;
            }
        }
    }
}

/* Fill the receive descriptor being clocked once it is full. Returns when it
 * will be, if that is still to come, or 0 to be looked at on the next poll. */
static int64_t i2s_receive(EspI2sMaster *s, int64_t now)
{
    uint32_t chan = 0, bytes = 0, next = 0;
    bool eof = false;

    if (!(s->regs[R_RX_CONF / 4] & RX_START)) {
        s->rx_armed = false;
        s->rx_start = 0;
        s->rx_said = false;
        s->rx_frames = 0;           /* the next capture hears the file anew */
        i2s_rx_idle(s, "stopped: RX_START clear", 0);
        return 0;
    }
    if (!i2s_find_channel(s, ESP_GDMA_IN_IDX, &s->rx_armed, &s->rx_cursor,
                          &chan)) {
        s->rx_start = 0;
        for (uint32_t i = 0; i < ESP32S3_GDMA_CHANNEL_COUNT; i++) {
            if ((esp_gdma_read_chan_register(s->gdma, ESP_GDMA_IN_IDX, i,
                                             GDMA_PERI_SEL_REG) & PERI_SEL_MASK)
                == s->periph) {
                i2s_rx_idle(s, "waiting: its channel was never started", i);
                return 0;
            }
        }
        i2s_rx_idle(s, "waiting: no GDMA channel bound to this block", 0);
        return 0;
    }
    bool check_owner = esp_gdma_read_chan_register(s->gdma, ESP_GDMA_IN_IDX,
                                                   chan, GDMA_CONF1_REG)
                       & GDMA_CHECK_OWNER;
    if (!i2s_descriptor(s->rx_cursor, true, check_owner, &bytes, &next, &eof)) {
        /* The CPU still owns it. On real hardware that is an overrun and the
         * receiver stalls, which is what happens here too; what it would
         * have heard meanwhile is lost, not made up later. */
        s->rx_start = 0;
        i2s_rx_idle(s, "waiting: the CPU owns the next descriptor", chan);
        return 0;
    }
    i2s_rx_idle(s, "running", chan);

    double rate = i2s_rate(s, &I2S_RX, &s->rx_said, &s->rx_fmt);
    if (rate > 0) {
        if (!s->rx_start) {
            s->rx_start = now;
        }
        int64_t full = s->rx_start + i2s_clocked_ns(bytes, rate);
        if (now < full) {
            return full;
        }
        s->rx_start = full;     /* the next descriptor fills from here */
    }

    i2s_aim(s, ESP_GDMA_IN_IDX, chan, s->rx_cursor);

    /* Silence, unless a WAV file is the microphone and the stream's rate is
     * known (ADR 0146): what the firmware waits for is the transfer. */
    g_autofree uint8_t *silence = g_malloc0(bytes);
    if (rate > 0) {
        i2s_mic_fill(s, silence, bytes);
    }
    if (!esp_gdma_write_channel(s->gdma, chan, silence, bytes)) {
        if (!s->complained) {
            s->complained = true;
            warn_report("%s: GDMA refused %u receive bytes on channel %u",
                        s->name ? s->name : "i2s", bytes, chan);
        }
        return 0;
    }
    if (next) {
        s->rx_cursor = DRAM_TOP_BITS | (next & LINK_ADDR);
    }
    s->buffers++;
    s->bytes += bytes;
    return 0;
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
static bool i2s_tx_total(uint32_t head, uint32_t *total, uint32_t *after)
{
    uint8_t raw[DESC_BYTES];
    uint32_t at = head;

    *total = 0;
    *after = 0;
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
            *after = next;              /* where the ring carries on */
            return *total != 0;         /* end marker */
        }
        if (next == 0 || next == at) {
            return false;               /* ran out without one */
        }
        at = next;
    }
    return false;
}

/* Investigation hatch, off unless BOARDEMUL_I2S_TRACE is set: say why the
 * transmit side is not moving. A firmware whose audio buffers never drain
 * blocks on i2s_write and starves everything else, and the three ways that
 * happens here look identical from the guest. Logged on change, not per tick.
 */
static void i2s_idle(EspI2sMaster *s, const char *reason)
{
    static int enabled = -1;

    if (enabled < 0) {
        enabled = getenv("BOARDEMUL_I2S_TRACE") != NULL;
    }
    if (!enabled || s->idle_reason == reason) {
        return;
    }
    s->idle_reason = reason;
    warn_report("%s: transmit %s (tx_conf=0x%08x)", s->name ? s->name : "i2s",
                reason, s->regs[R_TX_CONF / 4]);
}

/* The same hatch for the receive side: a capture that never fills blocks the
 * firmware in i2s_read, and the ways that happens look identical from the
 * guest. */
static void i2s_rx_idle(EspI2sMaster *s, const char *reason, uint32_t chan)
{
    static int enabled = -1;

    if (enabled < 0) {
        enabled = getenv("BOARDEMUL_I2S_TRACE") != NULL;
    }
    if (!enabled || s->rx_idle_reason == reason) {
        return;
    }
    s->rx_idle_reason = reason;
    uint64_t sel = esp_gdma_read_chan_register(s->gdma, ESP_GDMA_IN_IDX, chan,
                                               GDMA_PERI_SEL_REG);
    uint64_t link = esp_gdma_read_chan_register(s->gdma, ESP_GDMA_IN_IDX, chan,
                                                GDMA_LINK_REG);
    warn_report("%s: receive %s (rx_conf=0x%08x, in ch%u sel=0x%llx "
                "link=0x%llx)", s->name ? s->name : "i2s", reason,
                s->regs[R_RX_CONF / 4], chan, (unsigned long long)sel,
                (unsigned long long)link);
}

/* Take the queued transmit list once its bytes have been clocked out. Returns
 * when they will have been, if that is still to come, or 0 to be looked at on
 * the next poll. */
static int64_t i2s_transmit(EspI2sMaster *s, int64_t now)
{
    uint32_t chan = 0;
    uint32_t total = 0;
    uint32_t after = 0;

    if (!(s->regs[R_TX_CONF / 4] & TX_START)) {
        s->tx_armed = false;
        s->tx_start = s->tx_end = 0;
        s->tx_said = false;
        i2s_speaker_close(s);       /* the next stream is the next file */
        i2s_idle(s, "stopped: TX_START clear");
        return 0;
    }
    if (!i2s_find_channel(s, ESP_GDMA_OUT_IDX, &s->tx_armed, &s->tx_head,
                          &chan)) {
        i2s_idle(s, "waiting: no GDMA channel bound to this block");
        return 0;
    }
    if (!i2s_tx_total(s->tx_head, &total, &after)) {
        i2s_idle(s, "waiting: descriptor list has no end marker we own");
        return 0;
    }
    i2s_idle(s, "running");

    double rate = i2s_rate(s, &I2S_TX, &s->tx_said, &s->tx_fmt);
    if (rate > 0) {
        /* A list starts clocking when the last one finished, or when it was
         * found if the transmitter had run dry -- that gap is not made up. */
        if (!s->tx_start) {
            s->tx_start = MAX(s->tx_end, now);
        }
        int64_t done = s->tx_start + i2s_clocked_ns(total, rate);
        if (now < done) {
            return done;
        }
        s->tx_end = done;
        s->tx_start = 0;
    }

    i2s_aim(s, ESP_GDMA_OUT_IDX, chan, s->tx_head);

    /* The samples are what the speaker plays: written to the stream's WAV
     * file when asked for and the rate is known (ADR 0146), else dropped. */
    g_autofree uint8_t *played = g_malloc(total);
    if (!esp_gdma_read_channel(s->gdma, chan, played, total)) {
        if (!s->complained) {
            s->complained = true;
            warn_report("%s: GDMA refused %u transmit bytes on channel %u",
                        s->name ? s->name : "i2s", total, chan);
        }
        return 0;
    }

    /* Where the next list comes from depends on what kind of transfer this is,
     * and getting it wrong stalls audio outright. A one-shot list ends with no
     * next pointer, and the driver arms the link again for the one after it. A
     * stream is a ring the driver arms once and then only refills: nothing ever
     * sets START again, so disarming here would leave the buffers full, the
     * firmware blocked in i2s_write, and every other task starved behind it.
     * Follow the ring instead and keep going while TX_START is set, which is
     * what the receive side already does. The owner bits are the handshake: a
     * buffer the CPU has not refilled is simply not ours yet. */
    if (rate > 0) {
        i2s_speaker_write(s, played, total);
    }
    if (after) {
        s->tx_head = DRAM_TOP_BITS | (after & LINK_ADDR);
    } else {
        s->tx_armed = false;
    }
    s->buffers++;
    s->bytes += total;
    return 0;
}

/* Investigation hatch, off unless BOARDEMUL_PC_SAMPLE is set: on every tick,
 * log each core's PC with the virtual clock. A sampling profiler that costs
 * the guest nothing, unlike -d int, which slows it enough that a firmware can
 * take minutes of wall time to reach the state being studied. */
static void i2s_sample_pcs(void)
{
    static int enabled = -1;
    CPUState *cs;

    if (enabled < 0) {
        enabled = getenv("BOARDEMUL_PC_SAMPLE") != NULL;
    }
    if (!enabled) {
        return;
    }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    CPU_FOREACH(cs) {
        CPUClass *cc = CPU_GET_CLASS(cs);
        qemu_log_mask(LOG_UNIMP, "PCSAMPLE t=%lld cpu=%d halted=%d pc=0x%08llx\n",
                      (long long)now, cs->cpu_index, (int)cs->halted,
                      (unsigned long long)cc->get_pc(cs));
    }
}

static void i2s_tick(void *opaque)
{
    EspI2sMaster *s = opaque;

    i2s_sample_pcs();

    if (!s->gdma) {
        return;
    }

    /* Each direction says when it next has something to do. A buffer that
     * completed may have the next one due at once, so go round until neither
     * does, within reason. */
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    int64_t wake = now + I2S_POLL_NS;
    for (int pass = 0; pass < 64; pass++) {
        uint64_t before = s->buffers;
        int64_t rx = i2s_receive(s, now);
        int64_t tx = i2s_transmit(s, now);
        if (rx) {
            wake = MIN(wake, rx);
        }
        if (tx) {
            wake = MIN(wake, tx);
        }
        if (s->buffers == before) {
            break;
        }
    }
    timer_mod_ns(s->timer, wake);
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
     * full of them. The start bits and each direction's clock and frame
     * are read back, to pace the streams (ADR 0119).
     *
     * Except RX_UPDATE and TX_UPDATE, bit 8 of each CONF register, which the
     * TRM marks self-clearing: hardware clears it once the configuration has
     * crossed into the I2S clock domain, and ESP-IDF 4.4's i2s_hal_start_rx
     * spins until it has. Here the crossing is immediate (ADR 0146). */
    if (addr == R_RX_CONF || addr == R_TX_CONF) {
        value &= ~CONF_UPDATE;
    }
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
    if (s->mic && s->mic[0] && !i2s_mic_load(s, errp)) {
        return;
    }

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
    /* ADR 0146: a path prefix for the WAV files played streams go to, and a
     * WAV file the microphone hears. */
    DEFINE_PROP_STRING("speaker", EspI2sMaster, speaker),
    DEFINE_PROP_STRING("mic", EspI2sMaster, mic),
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
