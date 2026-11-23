/*
 * SPDX-FileCopyrightText: 2026 Prolific Lab
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
/*
 * esp_i2c_master -- ESP32-S3 I2C master controller.
 *
 * SoC silicon (ADR 0011). It knows how a command list is assembled and how the
 * FIFOs behave; it knows nothing about any device on the bus.
 *
 * Batching is on the transaction, which is what I2C itself defines: everything
 * from a START to the STOP that ends it. The driver builds that out of roughly
 * twenty-four register accesses, and measured on the factory firmware I2C was
 * the whole of the remaining bridge traffic -- 930,502 accesses for 39,128
 * transactions. Here the registers are serviced natively and one message
 * carries the transaction.
 *
 * Register offsets and bit positions from
 * components/soc/esp32s3/include/soc/i2c_reg.h, and the command opcodes from
 * components/hal/esp32s3/include/hal/i2c_ll.h -- they are not the ESP32's.
 */

#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "qemu/log.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "hw/irq.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qapi/error.h"
#include "qom/object.h"
#include "qemu/main-loop.h"
#include "hw/misc/esp_board_glue.h"
#include "sysemu/reset.h"
#include "qemu/timer.h"

#include <sys/socket.h>
#include <sys/un.h>

OBJECT_DECLARE_SIMPLE_TYPE(EspI2cMaster, ESP_I2C_MASTER)

#define R_SCL_LOW    0x00
#define R_CTR        0x04
#define R_SR         0x08
#define R_TO         0x0C
#define R_SLAVE_ADDR 0x10
#define R_FIFO_ST    0x14
#define R_FIFO_CONF  0x18
#define R_DATA       0x1C
#define R_INT_RAW    0x20
#define R_INT_CLR    0x24
#define R_INT_ENA    0x28
#define R_INT_STATUS 0x2C
#define R_SCL_HIGH         0x38
#define R_SCL_START_HOLD   0x40
#define R_SCL_RSTART_SETUP 0x44
#define R_SCL_STOP_HOLD    0x48
#define R_SCL_STOP_SETUP   0x4C
#define R_CLK_CONF         0x54
#define R_COMD0      0x58
#define R_DATE       0xF8
#define TXFIFO_BASE  0x100
#define RXFIFO_BASE  0x180

#define CTR_MS_MODE      (1u << 4)
#define CTR_TRANS_START  (1u << 5)
#define CTR_FSM_RST      (1u << 10)
#define CTR_CONF_UPGATE  (1u << 11)

#define INT_RXFIFO_WM      (1u << 0)
#define INT_END_DETECT     (1u << 3)
#define INT_TRANS_COMPLETE (1u << 7)
#define INT_NACK           (1u << 10)

#define FIFO_CONF_RX_RST (1u << 12)
#define FIFO_CONF_TX_RST (1u << 13)

/* ESP32-S3 opcodes; the ESP32's are different and RESTART is not 0. */
#define CMD_RSTART 6
#define CMD_WRITE  1
#define CMD_READ   3
#define CMD_STOP   2
#define CMD_END    4
#define CMD_DONE   (1u << 31)

/* What sets the bus's pace (ADR 0121), from ESP-IDF v4.4.6's
 * soc/esp32s3/include/soc/i2c_struct.h: nine-bit periods in I2C_SCLK cycles,
 * and the clock's own fractional divider. */
#define PERIOD(v)              ((v) & 0x1FF)
#define SCL_WAIT_HIGH(v)       (((v) >> 9) & 0x7F)
#define CLK_DIV_NUM(v)         ((v) & 0xFF)
#define CLK_DIV_A(v)           (((v) >> 8) & 0x3F)
#define CLK_DIV_B(v)           (((v) >> 14) & 0x3F)
#define CLK_SEL_RC_FAST        (1u << 20)

#define I2C_REG_COUNT  (0x1000 / 4)
#define I2C_FIFO_DEPTH 32
#define I2C_XFER_MAX   1024

typedef struct QEMU_PACKED {
    uint8_t addr;       /* seven-bit device address */
    uint8_t flags;      /* bit 0: the driver asked for the ack to be checked */
    uint16_t wlen;
    uint16_t rlen;
    uint16_t pad;
} EspI2cRequest;

typedef struct QEMU_PACKED {
    uint8_t ack;        /* zero means nobody answered */
    uint8_t pad[3];
} EspI2cReply;

struct EspI2cMaster {
    DeviceState parent_obj;

    MemoryRegion iomem;
    char *socket_path;
    char *region_name;
    char *intc_type;
    uint64_t base;
    uint64_t size;
    int32_t priority;
    int32_t irq_source;

    uint32_t regs[I2C_REG_COUNT];
    uint32_t int_raw;

    uint8_t txfifo[I2C_FIFO_DEPTH];
    uint32_t tx_count;
    uint8_t rxfifo[I2C_FIFO_DEPTH];
    uint32_t rx_count;
    uint32_t rx_pos;

    /* Assembled across however many command lists the driver splits the
     * transaction into: the address survives an END, only STOP ends it. */
    int cur_addr;
    bool addressing;
    uint8_t pending[I2C_XFER_MAX];
    uint32_t pending_len;
    bool ack_check;
    bool exchanged;     /* something already went out for this transaction */
    uint32_t read_total;

    qemu_irq irq_line;
    int irq_level;
    QEMUTimer *irq_timer;          /* the deferred interrupt (ESP_IRQ_DEFER_NS) */
    /* Completion bits raised by a command list still on the bus, and when it
     * comes off it (ADR 0121). */
    uint32_t int_pending;
    int64_t int_at;
    double scl_said;
    int fd;
    bool broken;
};

static bool i2c_send_all(EspI2cMaster *s, const void *buf, size_t len)
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

static bool i2c_recv_all(EspI2cMaster *s, void *buf, size_t len)
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

static void i2c_fail(EspI2cMaster *s, const char *what)
{
    if (!s->broken) {
        s->broken = true;
        error_report("esp-i2c-master(%s): %s; the bus is inert from now on",
                     s->region_name ? s->region_name : "?", what);
    }
}

/* Fold in the completion bits of a command list whose bus time has passed.
 * Asked whenever they could be seen, on the guest's own clock. */
static void i2c_settle(EspI2cMaster *s)
{
    if (s->int_pending
        && qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) >= s->int_at) {
        s->int_raw |= s->int_pending;
        s->int_pending = 0;
    }
}

static void i2c_irq_due(void *opaque)
{
    EspI2cMaster *s = opaque;

    i2c_settle(s);
    int level = (s->int_raw & s->regs[R_INT_ENA / 4]) ? 1 : 0;

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
static void i2c_reset(void *opaque)
{
    EspI2cMaster *s = opaque;

    if (esp_reset_was_digital()) {
        /* Back to what the first boot found (ADR 0154). */
        memset(s->regs, 0, sizeof(s->regs));
        s->int_raw = 0;
        s->int_pending = 0;
        s->int_at = 0;
        s->tx_count = s->rx_count = s->rx_pos = 0;
        s->cur_addr = -1;
        s->addressing = false;
        s->pending_len = 0;
        s->ack_check = false;
        s->exchanged = false;
        s->read_total = 0;
    }
    s->irq_level = -1;
    i2c_irq_due(s);
}

/* Deferred rather than raised from inside the MMIO handler.
 *
 * A transaction completes during the guest's own write to CTR, so raising the
 * line there delivers the interrupt on the very next instruction -- before the
 * driver has finished the function that started the transfer. That re-enters
 * it, and the recursion shows up as a stack-canary trip in whichever task was
 * unlucky, nowhere near the I2C code. It was a bottom half; it is now a fixed
 * delay on the guest's clock, so the instruction it lands on does not depend
 * on the host (ESP_IRQ_DEFER_NS, ADR 0113). */
static void i2c_update_irq(EspI2cMaster *s)
{
    int64_t at = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ESP_IRQ_DEFER_NS;

    if (s->int_pending) {
        at = MAX(at, s->int_at);    /* when the bits it would carry appear */
    }
    if (s->irq_timer && (!timer_pending(s->irq_timer)
                         || (int64_t)timer_expire_time_ns(s->irq_timer) < at)) {
        timer_mod_ns(s->irq_timer, at);
    }
}

/* I2C_SCLK (TRM 27.4.1): XTAL_CLK or RC_FAST_CLK, over
 * DIV_NUM + 1 + DIV_A / DIV_B. */
static double i2c_sclk_hz(EspI2cMaster *s)
{
    uint32_t conf = s->regs[R_CLK_CONF / 4];
    double divisor = CLK_DIV_NUM(conf) + 1.0;

    if (CLK_DIV_B(conf)) {
        divisor += (double)CLK_DIV_A(conf) / CLK_DIV_B(conf);
    }
    return ((conf & CLK_SEL_RC_FAST) ? 17.5e6 : 40e6) / divisor;
}

/* One SCL period in I2C_SCLK cycles, when nothing stretches it (27.4). */
static uint32_t i2c_scl_cycles(EspI2cMaster *s)
{
    uint32_t high = s->regs[R_SCL_HIGH / 4];

    return PERIOD(s->regs[R_SCL_LOW / 4]) + PERIOD(high) + SCL_WAIT_HIGH(high)
           + 3;
}

/* The I2C_SCLK cycles one command puts on the bus (ADR 0121). */
static uint64_t i2c_command_cycles(EspI2cMaster *s, uint32_t op, uint32_t count)
{
    switch (op) {
    case CMD_RSTART:
        return PERIOD(s->regs[R_SCL_RSTART_SETUP / 4]) + 1
               + PERIOD(s->regs[R_SCL_START_HOLD / 4]) + 1;
    case CMD_WRITE:
    case CMD_READ:
        return (uint64_t)count * 9 * i2c_scl_cycles(s);
    case CMD_STOP:
        return PERIOD(s->regs[R_SCL_STOP_SETUP / 4]) + 1
               + PERIOD(s->regs[R_SCL_STOP_HOLD / 4]) + 1;
    default:
        return 0;
    }
}

/* One exchange with the model: whatever has been written so far, plus however
 * many bytes are wanted back. Returns false if nobody acknowledged. */
static bool i2c_exchange(EspI2cMaster *s, uint32_t rlen)
{
    EspI2cRequest req;
    EspI2cReply reply;

    if (s->cur_addr < 0 || s->broken || s->fd < 0) {
        return false;
    }
    if (rlen > I2C_FIFO_DEPTH) {
        rlen = I2C_FIFO_DEPTH;
    }
    req.addr = (uint8_t)s->cur_addr;
    req.flags = s->ack_check ? 1 : 0;
    req.wlen = (uint16_t)s->pending_len;
    req.rlen = (uint16_t)rlen;
    req.pad = 0;

    if (!i2c_send_all(s, &req, sizeof(req)) ||
        (s->pending_len && !i2c_send_all(s, s->pending, s->pending_len)) ||
        !i2c_recv_all(s, &reply, sizeof(reply))) {
        i2c_fail(s, "exchange failed");
        return false;
    }
    s->pending_len = 0;
    s->exchanged = true;
    if (rlen) {
        if (!i2c_recv_all(s, s->rxfifo, rlen)) {
            i2c_fail(s, "short read");
            return false;
        }
        s->rx_count = rlen;
        s->rx_pos = 0;
    }
    return reply.ack != 0;
}

static void i2c_execute(EspI2cMaster *s)
{
    bool nacked = false;

    if (!(s->regs[R_CTR / 4] & CTR_MS_MODE)) {
        s->int_raw |= INT_TRANS_COMPLETE;
        i2c_update_irq(s);
        return;
    }
    s->int_raw &= ~(INT_NACK | INT_TRANS_COMPLETE | INT_END_DETECT);
    uint32_t before = s->int_raw;
    uint64_t cycles = 0;

    for (int slot = 0; slot < 8; slot++) {
        uint32_t cmd = s->regs[(R_COMD0 + slot * 4) / 4];
        uint32_t op = (cmd >> 11) & 0x7;
        uint32_t count = cmd & 0xFF;

        cycles += i2c_command_cycles(s, op, count);

        s->ack_check = (cmd & (1u << 8)) != 0;

        switch (op) {
        case CMD_RSTART:
            s->addressing = true;
            break;

        case CMD_WRITE: {
            uint32_t take = count > s->tx_count ? s->tx_count : count;
            const uint8_t *src = s->txfifo;

            if (s->addressing && take) {
                s->cur_addr = src[0] >> 1;
                s->addressing = false;
                src++;
                take--;
            }
            if (take && s->pending_len + take <= I2C_XFER_MAX) {
                memcpy(s->pending + s->pending_len, src, take);
                s->pending_len += take;
            }
            /* Consume what the command claimed, not what we used. */
            if (count >= s->tx_count) {
                s->tx_count = 0;
            } else {
                memmove(s->txfifo, s->txfifo + count, s->tx_count - count);
                s->tx_count -= count;
            }
            break;
        }

        case CMD_READ:
            /* Accumulated, not exchanged here. A driver reading n bytes emits
             * two READ commands -- n-1 acknowledged and the last one not -- and
             * exchanging on each would ask the device for two separate replies,
             * so it would answer the second from a freshly built one. One
             * transaction is one question. */
            s->read_total += count;
            break;

        case CMD_STOP:
            /* Only speak if there is something left to say. A read has already
             * been exchanged; an address-only probe -- which is what a bus scan
             * is -- has not, and its whole purpose is to see the acknowledgement.
             */
            if (s->pending_len || s->read_total || !s->exchanged) {
                if (!i2c_exchange(s, s->read_total)) {
                    nacked = true;
                }
            }
            s->cur_addr = -1;
            s->pending_len = 0;
            s->read_total = 0;
            s->exchanged = false;
            s->regs[(R_COMD0 + slot * 4) / 4] |= CMD_DONE;
            s->int_raw |= INT_TRANS_COMPLETE;
            goto done;

        case CMD_END:
            /* The driver refills the command list and starts again; the address
             * and anything pending carry over. Reads must be satisfied now
             * though: it will look at the FIFO before refilling. */
            if (s->read_total) {
                if (!i2c_exchange(s, s->read_total)) {
                    nacked = true;
                }
                s->read_total = 0;
            }
            s->regs[(R_COMD0 + slot * 4) / 4] |= CMD_DONE;
            s->int_raw |= INT_END_DETECT;
            goto done;

        default:
            break;
        }

        s->regs[(R_COMD0 + slot * 4) / 4] |= CMD_DONE;
        if (nacked) {
            break;
        }
    }
    if (!(s->int_raw & (INT_TRANS_COMPLETE | INT_END_DETECT))) {
        s->int_raw |= INT_TRANS_COMPLETE;
    }
done:
    if (nacked) {
        s->int_raw |= INT_NACK | INT_TRANS_COMPLETE;
        s->cur_addr = -1;
        s->pending_len = 0;
        s->read_total = 0;
        s->exchanged = false;
    }
    if (s->rx_count > s->rx_pos) {
        s->int_raw |= INT_RXFIFO_WM;
    }

    /* What this list raised appears once its bus time has passed. */
    double sclk = i2c_sclk_hz(s);
    double scl = sclk / i2c_scl_cycles(s);
    if (scl != s->scl_said) {
        s->scl_said = scl;
        qemu_log_mask(LOG_UNIMP, "esp-i2c-master(%s): SCL at %.0f Hz\n",
                      s->region_name ? s->region_name : "?", scl);
    }
    uint32_t fresh = s->int_raw & ~before;
    s->int_raw &= ~fresh;
    s->int_pending |= fresh;
    s->int_at = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                + (int64_t)(cycles * 1e9 / sclk);
    i2c_update_irq(s);
}

static uint64_t i2c_read(void *opaque, hwaddr offset, unsigned size)
{
    EspI2cMaster *s = opaque;

    if (offset >= RXFIFO_BASE && offset < RXFIFO_BASE + I2C_FIFO_DEPTH) {
        uint32_t i = offset - RXFIFO_BASE;
        return i < s->rx_count ? s->rxfifo[i] : 0;
    }
    if (offset >= TXFIFO_BASE && offset < TXFIFO_BASE + I2C_FIFO_DEPTH) {
        return 0;
    }
    switch (offset) {
    case R_DATA:
        return s->rx_pos < s->rx_count ? s->rxfifo[s->rx_pos++] : 0;
    case R_INT_RAW:
        i2c_settle(s);
        return s->int_raw;
    case R_INT_STATUS:
        i2c_settle(s);
        return s->int_raw & s->regs[R_INT_ENA / 4];
    case R_FIFO_ST:
        return ((s->rx_count - s->rx_pos) & 0x3F) << 8 | (s->tx_count & 0x3F) << 18;
    case R_SR:
        /* Transactions complete inside the TRANS_START write, so the bus is
         * always idle by the time the guest looks. */
        return 0;
    case R_DATE:
        return 0x19071700;
    default:
        return offset / 4 < I2C_REG_COUNT ? s->regs[offset / 4] : 0;
    }
}

static void i2c_write(void *opaque, hwaddr offset, uint64_t val, unsigned size)
{
    EspI2cMaster *s = opaque;
    uint32_t value = (uint32_t)val;

    if (offset >= TXFIFO_BASE && offset < TXFIFO_BASE + I2C_FIFO_DEPTH) {
        if (s->tx_count < I2C_FIFO_DEPTH) {
            s->txfifo[s->tx_count++] = value & 0xFF;
        }
        return;
    }
    if (offset >= RXFIFO_BASE && offset < RXFIFO_BASE + I2C_FIFO_DEPTH) {
        return;
    }
    switch (offset) {
    case R_DATA:
        if (s->tx_count < I2C_FIFO_DEPTH) {
            s->txfifo[s->tx_count++] = value & 0xFF;
        }
        return;
    case R_INT_CLR:
        /* Whether or not it has appeared yet (ADR 0121). */
        s->int_raw &= ~value;
        s->int_pending &= ~value;
        i2c_update_irq(s);
        return;
    case R_INT_ENA:
        s->regs[R_INT_ENA / 4] = value;
        i2c_update_irq(s);
        return;
    case R_FIFO_CONF:
        if (value & FIFO_CONF_TX_RST) {
            s->tx_count = 0;
        }
        if (value & FIFO_CONF_RX_RST) {
            s->rx_count = s->rx_pos = 0;
        }
        s->regs[offset / 4] = value;
        return;
    case R_CTR:
        /* TRANS_START, FSM_RST and CONF_UPGATE are self-clearing triggers.
         * Storing them makes the driver's own read-modify-write re-fire the
         * transaction, which corrupts the FIFOs. */
        s->regs[R_CTR / 4] =
            value & ~(CTR_TRANS_START | CTR_FSM_RST | CTR_CONF_UPGATE);
        if (value & CTR_FSM_RST) {
            s->tx_count = s->rx_count = s->rx_pos = 0;
        }
        if (value & CTR_TRANS_START) {
            i2c_execute(s);
        }
        return;
    default:
        if (offset / 4 < I2C_REG_COUNT) {
            s->regs[offset / 4] = value;
        }
        return;
    }
}

static const MemoryRegionOps i2c_ops = {
    .read = i2c_read,
    .write = i2c_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

static void i2c_realize(DeviceState *dev, Error **errp)
{
    EspI2cMaster *s = ESP_I2C_MASTER(dev);
    struct sockaddr_un addr;

    if (!s->socket_path) {
        error_setg(errp, "esp-i2c-master: 'socket' property is required");
        return;
    }
    if (strlen(s->socket_path) >= sizeof(addr.sun_path)) {
        error_setg(errp, "esp-i2c-master: socket path too long");
        return;
    }
    if (s->irq_source >= 0) {
        Object *intc = object_resolve_path_type("", s->intc_type, NULL);
        if (!intc) {
            error_setg(errp, "esp-i2c-master: no '%s' in the machine",
                       s->intc_type);
            return;
        }
        s->irq_line = qdev_get_gpio_in(DEVICE(intc), s->irq_source);
        s->irq_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, i2c_irq_due, s);
        qemu_register_reset(i2c_reset, s);
    }

    s->fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (s->fd < 0) {
        error_setg_errno(errp, errno, "esp-i2c-master: socket()");
        return;
    }
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    pstrcpy(addr.sun_path, sizeof(addr.sun_path), s->socket_path);
    if (connect(s->fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        error_setg_errno(errp, errno, "esp-i2c-master: connect(%s)",
                         s->socket_path);
        close(s->fd);
        s->fd = -1;
        return;
    }

    memory_region_init_io(&s->iomem, OBJECT(s), &i2c_ops, s,
                          s->region_name ? s->region_name : TYPE_ESP_I2C_MASTER,
                          s->size);
    memory_region_add_subregion_overlap(get_system_memory(), s->base,
                                        &s->iomem, s->priority);
}

static Property i2c_props[] = {
    DEFINE_PROP_STRING("socket", EspI2cMaster, socket_path),
    DEFINE_PROP_STRING("name", EspI2cMaster, region_name),
    DEFINE_PROP_STRING("intc", EspI2cMaster, intc_type),
    DEFINE_PROP_UINT64("base", EspI2cMaster, base, 0x60013000),
    DEFINE_PROP_UINT64("size", EspI2cMaster, size, 0x1000),
    DEFINE_PROP_INT32("priority", EspI2cMaster, priority, 100),
    DEFINE_PROP_INT32("irq", EspI2cMaster, irq_source, -1),
    DEFINE_PROP_END_OF_LIST(),
};

static void i2c_instance_init(Object *obj)
{
    EspI2cMaster *s = ESP_I2C_MASTER(obj);

    s->intc_type = g_strdup("misc.esp32s3.intmatrix");
    s->irq_level = -1;
    s->fd = -1;
    s->cur_addr = -1;
}

static void i2c_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = i2c_realize;
    dc->user_creatable = true;
    device_class_set_props(dc, i2c_props);
}

static const TypeInfo i2c_info = {
    .name = TYPE_ESP_I2C_MASTER,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspI2cMaster),
    .instance_init = i2c_instance_init,
    .class_init = i2c_class_init,
};

static void i2c_register_types(void)
{
    type_register_static(&i2c_info);
}

type_init(i2c_register_types)
