/*
 * esp_gpio_block -- ESP32-S3 GPIO: output levels, input levels, interrupts.
 *
 * SoC silicon, not board hardware (ADR 0011). It knows what a pin is and what
 * an edge is; it knows nothing about what any pin is wired to.
 *
 * It lives in C rather than in the Python tier for two reasons. Chip-select and
 * data/command are ordinary GPIOs on this board, so the SPI model has to read
 * pin levels on every transfer -- that cannot cross a socket. And the interrupt
 * path has to be able to fire while the guest is doing something else entirely.
 *
 * Register offsets and bit positions from
 * components/soc/esp32s3/include/soc/gpio_reg.h.
 */

#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "qemu/main-loop.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "hw/irq.h"
#include "qemu/log.h"
#include "qemu/timer.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qapi/error.h"
#include "qom/object.h"
#include "hw/misc/esp_board_glue.h"
#include "sysemu/reset.h"

#include <sys/socket.h>
#include <sys/un.h>

OBJECT_DECLARE_SIMPLE_TYPE(EspGpioBlock, ESP_GPIO_BLOCK)

#define GPIO_PIN_COUNT 48
#define GPIO_REG_COUNT (0x1000 / 4)
#define GPIO_WATCH_MAX 8

/* Offsets are expressed from the block base throughout, so the two claimed
 * windows can share one decoder. */
#define R_OUT            0x04
#define R_OUT_W1TS       0x08
#define R_OUT_W1TC       0x0C
#define R_OUT1           0x10
#define R_OUT1_W1TS      0x14
#define R_OUT1_W1TC      0x18
#define R_ENABLE         0x20
#define R_ENABLE_W1TS    0x24
#define R_ENABLE_W1TC    0x28
#define R_STRAP          0x38
#define R_IN             0x3C
#define R_IN1            0x40
#define R_STATUS         0x44
#define R_STATUS_W1TS    0x48
#define R_STATUS_W1TC    0x4C
#define R_STATUS1        0x50
#define R_STATUS1_W1TS   0x54
#define R_STATUS1_W1TC   0x58
#define R_PCPU_INT       0x5C
#define R_PCPU_NMI_INT   0x60
#define R_PCPU_INT1      0x68
#define R_PCPU_NMI_INT1  0x6C
#define R_PIN0           0x74

/* GPIO_PINn: INT_TYPE at bits 9:7, INT_ENA at bits 17:13. Bit 0 of INT_ENA is
 * the PRO-CPU interrupt; the rest are NMI and APP CPU. */
#define PIN_INT_TYPE_SHIFT 7
#define PIN_INT_TYPE_MASK  0x7
#define PIN_INT_ENA_SHIFT  13
#define PIN_INT_ENA_MASK   0x1F
#define INT_ENA_PRO_CPU    0x1

enum {
    INTR_DISABLE = 0,
    INTR_POSEDGE = 1,
    INTR_NEGEDGE = 2,
    INTR_ANYEDGE = 3,
    INTR_LOW_LEVEL = 4,
    INTR_HIGH_LEVEL = 5,
};

typedef struct {
    unsigned pin;
    EspGpioWatchFn fn;
    void *opaque;
} EspGpioWatch;

/* One pin's pending change, made on the guest's clock (ADR 0112). The timer is
 * created the first time the pin is scheduled. */
typedef struct {
    EspGpioBlock *block;
    unsigned pin;
    int level;
    QEMUTimer *timer;
} EspGpioScheduled;

/* Message from the host: drive one input pin. Fixed size, no framing. */
typedef struct QEMU_PACKED {
    uint32_t pin;
    uint32_t level;
} EspGpioInputMessage;

struct EspGpioBlock {
    DeviceState parent_obj;

    /* Two windows rather than one: GPIO_STRAP sits between them and is the
     * single register the machine's own GPIO model implements. The ROM reads it
     * at boot, so it is left alone. */
    MemoryRegion iomem_lo;
    MemoryRegion iomem_hi;

    char *intc_type;
    char *socket_path;
    uint64_t base;
    int32_t priority;
    int32_t irq_source;

    uint64_t out_level;
    uint64_t in_level;
    uint64_t in_driven;      /* which pins the host is driving */
    uint64_t enable;
    uint64_t status;
    uint64_t enabled_mask;   /* cached: pins with the PRO-CPU interrupt on */
    uint32_t pin_cfg[GPIO_PIN_COUNT];
    /* Everything not decoded above -- most of it the pin matrix. Storing it is
     * not modelling: it is the difference between a register that remembers
     * what the driver wrote and one that silently reads back zero, which is how
     * a read-modify-write turns into a corrupted configuration (ADR 0008). */
    uint32_t regs[GPIO_REG_COUNT];

    EspGpioWatch watches[GPIO_WATCH_MAX];
    unsigned watch_count;
    EspGpioScheduled scheduled[GPIO_PIN_COUNT];

    qemu_irq irq_line;
    int irq_level;
    int fd;
};

static EspGpioBlock *the_block;   /* set at realize; see esp_gpio_pin_level */

static uint64_t gpio_levels(EspGpioBlock *s)
{
    return (s->out_level & ~s->in_driven) | (s->in_level & s->in_driven);
}

int esp_gpio_pin_level(DeviceState *dev, unsigned pin)
{
    EspGpioBlock *s = dev ? ESP_GPIO_BLOCK(dev) : the_block;

    if (!s || pin >= GPIO_PIN_COUNT) {
        return 0;
    }
    return (gpio_levels(s) >> pin) & 1;
}

void esp_gpio_watch(DeviceState *dev, unsigned pin,
                    EspGpioWatchFn fn, void *opaque)
{
    EspGpioBlock *s = dev ? ESP_GPIO_BLOCK(dev) : the_block;

    if (!s || s->watch_count >= GPIO_WATCH_MAX) {
        return;
    }
    s->watches[s->watch_count].pin = pin;
    s->watches[s->watch_count].fn = fn;
    s->watches[s->watch_count].opaque = opaque;
    s->watch_count++;
}

static void gpio_recompute_enabled(EspGpioBlock *s)
{
    uint64_t mask = 0;

    for (unsigned pin = 0; pin < GPIO_PIN_COUNT; pin++) {
        uint32_t ena = (s->pin_cfg[pin] >> PIN_INT_ENA_SHIFT) & PIN_INT_ENA_MASK;
        if (ena & INT_ENA_PRO_CPU) {
            mask |= 1ULL << pin;
        }
    }
    s->enabled_mask = mask;
}

/* Callers already hold the BQL: MMIO handlers do, and so does the main-loop
 * callback that services the host socket. */
static void gpio_update_irq(EspGpioBlock *s)
{
    int level = (s->status & s->enabled_mask) ? 1 : 0;

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
static void gpio_reset(void *opaque)
{
    EspGpioBlock *s = opaque;

    s->irq_level = -1;
    gpio_update_irq(s);
}

static void gpio_latch(EspGpioBlock *s, unsigned pin, int before, int after)
{
    uint32_t type = (s->pin_cfg[pin] >> PIN_INT_TYPE_SHIFT) & PIN_INT_TYPE_MASK;
    bool rising = !before && after;
    bool falling = before && !after;
    bool fires =
        (type == INTR_POSEDGE && rising) ||
        (type == INTR_NEGEDGE && falling) ||
        (type == INTR_ANYEDGE && (rising || falling)) ||
        (type == INTR_LOW_LEVEL && !after) ||
        (type == INTR_HIGH_LEVEL && after);

    if (fires) {
        s->status |= 1ULL << pin;
        gpio_update_irq(s);
    }
}

static void gpio_levels_changed(EspGpioBlock *s, uint64_t before)
{
    uint64_t after = gpio_levels(s);
    uint64_t changed = before ^ after;

    if (!changed) {
        return;
    }
    for (unsigned pin = 0; pin < GPIO_PIN_COUNT; pin++) {
        if (!(changed & (1ULL << pin))) {
            continue;
        }
        gpio_latch(s, pin, (before >> pin) & 1, (after >> pin) & 1);
        for (unsigned w = 0; w < s->watch_count; w++) {
            if (s->watches[w].pin == pin) {
                s->watches[w].fn(s->watches[w].opaque, pin, (after >> pin) & 1);
            }
        }
    }
}

static uint64_t gpio_read(void *opaque, hwaddr offset, unsigned size)
{
    EspGpioBlock *s = opaque;
    hwaddr reg = offset;

    if (reg >= R_PIN0 && reg < R_PIN0 + GPIO_PIN_COUNT * 4) {
        return s->pin_cfg[(reg - R_PIN0) / 4];
    }
    switch (reg) {
    case R_OUT:           return s->out_level & 0xFFFFFFFF;
    case R_OUT1:          return (s->out_level >> 32) & 0xFFFFFFFF;
    case R_ENABLE:        return s->enable & 0xFFFFFFFF;
    case R_IN:            return gpio_levels(s) & 0xFFFFFFFF;
    case R_IN1:           return (gpio_levels(s) >> 32) & 0xFFFFFFFF;
    case R_STATUS:        return s->status & 0xFFFFFFFF;
    case R_STATUS1:       return (s->status >> 32) & 0xFFFFFFFF;
    case R_PCPU_INT:      return (s->status & s->enabled_mask) & 0xFFFFFFFF;
    case R_PCPU_INT1:     return ((s->status & s->enabled_mask) >> 32) & 0xFFFFFFFF;
    case R_PCPU_NMI_INT:
    case R_PCPU_NMI_INT1: return 0;
    /* Write-one-to-set/clear registers read back as zero. */
    case R_OUT_W1TS: case R_OUT_W1TC:
    case R_OUT1_W1TS: case R_OUT1_W1TC:
    case R_STATUS_W1TS: case R_STATUS_W1TC:
    case R_STATUS1_W1TS: case R_STATUS1_W1TC:
        return 0;
    default:
        return reg / 4 < GPIO_REG_COUNT ? s->regs[reg / 4] : 0;
    }
}

/* Investigation hatch, off unless BOARDEMUL_GPIO_TRACE is set: report every
 * line the guest drives, and which way. Which pin is a board's chip select is
 * not written down anywhere for a board nobody has documented; it is the pin
 * that goes low around the transaction, so this is how to find it (ADR 0006).
 */
static void gpio_trace_levels(uint64_t before, uint64_t after)
{
    static int enabled = -1;
    uint64_t changed;

    if (enabled < 0) {
        enabled = getenv("BOARDEMUL_GPIO_TRACE") != NULL;
    }
    if (!enabled) {
        return;
    }
    changed = before ^ after;
    for (unsigned pin = 0; pin < GPIO_PIN_COUNT; pin++) {
        if (changed & (1ULL << pin)) {
            qemu_log_mask(LOG_UNIMP, "GPIO pin %u -> %d%c", pin,
                          (int)((after >> pin) & 1), 10);
        }
    }
}

static void gpio_write(void *opaque, hwaddr offset, uint64_t val, unsigned size)
{
    EspGpioBlock *s = opaque;
    uint32_t value = (uint32_t)val;
    uint64_t before = gpio_levels(s);
    hwaddr reg = offset;

    if (reg >= R_PIN0 && reg < R_PIN0 + GPIO_PIN_COUNT * 4) {
        unsigned pin = (reg - R_PIN0) / 4;
        uint32_t type;

        s->pin_cfg[pin] = value;
        gpio_recompute_enabled(s);
        /* A level trigger on a line already at that level latches at once. An
         * edge trigger must not: synthesising one here fires before any handler
         * exists, and nothing would ever clear it. */
        type = (value >> PIN_INT_TYPE_SHIFT) & PIN_INT_TYPE_MASK;
        if (type == INTR_LOW_LEVEL || type == INTR_HIGH_LEVEL) {
            int level = (gpio_levels(s) >> pin) & 1;
            gpio_latch(s, pin, !level, level);
        }
        gpio_update_irq(s);
        return;
    }

    switch (reg) {
    case R_OUT:         s->out_level = (s->out_level & ~0xFFFFFFFFULL) | value; break;
    case R_OUT_W1TS:    s->out_level |= value; break;
    case R_OUT_W1TC:    s->out_level &= ~(uint64_t)value; break;
    case R_OUT1:        s->out_level = (s->out_level & 0xFFFFFFFFULL) |
                                       ((uint64_t)value << 32); break;
    case R_OUT1_W1TS:   s->out_level |= (uint64_t)value << 32; break;
    case R_OUT1_W1TC:   s->out_level &= ~((uint64_t)value << 32); break;
    case R_ENABLE:      s->enable = (s->enable & ~0xFFFFFFFFULL) | value; break;
    case R_ENABLE_W1TS: s->enable |= value; break;
    case R_ENABLE_W1TC: s->enable &= ~(uint64_t)value; break;
    case R_STATUS:      s->status = (s->status & ~0xFFFFFFFFULL) | value; goto irq;
    case R_STATUS_W1TS: s->status |= value; goto irq;
    case R_STATUS_W1TC: s->status &= ~(uint64_t)value; goto irq;
    case R_STATUS1:     s->status = (s->status & 0xFFFFFFFFULL) |
                                    ((uint64_t)value << 32); goto irq;
    case R_STATUS1_W1TS: s->status |= (uint64_t)value << 32; goto irq;
    case R_STATUS1_W1TC: s->status &= ~((uint64_t)value << 32); goto irq;
    default:
        if (reg / 4 < GPIO_REG_COUNT) {
            s->regs[reg / 4] = value;
        }
        return;
    }
    gpio_trace_levels(before, gpio_levels(s));
    gpio_levels_changed(s, before);
    return;
irq:
    gpio_update_irq(s);
}

static const MemoryRegionOps gpio_ops = {
    .read = gpio_read,
    .write = gpio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

/* The low window starts at the block base, so its offsets already match the
 * register numbers. The high window starts at 0x3C and must be shifted back. */
static uint64_t gpio_hi_read(void *opaque, hwaddr offset, unsigned size)
{
    return gpio_read(opaque, offset + R_IN, size);
}

static void gpio_hi_write(void *opaque, hwaddr offset, uint64_t val, unsigned size)
{
    gpio_write(opaque, offset + R_IN, val, size);
}

static const MemoryRegionOps gpio_hi_ops = {
    .read = gpio_hi_read,
    .write = gpio_hi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

/* An input pin driven from outside the guest: by the host over the socket, or
 * by another device in the machine (esp_gpio_drive). Either way it overrides
 * whatever the guest left in the output register, and edges latch. */
static void gpio_drive_input(EspGpioBlock *s, unsigned pin, int level)
{
    uint64_t before;

    if (pin >= GPIO_PIN_COUNT) {
        return;
    }
    before = gpio_levels(s);
    s->in_driven |= 1ULL << pin;
    if (level) {
        s->in_level |= 1ULL << pin;
    } else {
        s->in_level &= ~(1ULL << pin);
    }
    gpio_levels_changed(s, before);
}

static void gpio_host_readable(void *opaque)
{
    EspGpioBlock *s = opaque;
    EspGpioInputMessage msg;
    uint8_t *p = (uint8_t *)&msg;
    size_t left = sizeof(msg);

    while (left) {
        ssize_t n = recv(s->fd, p, left, 0);
        if (n <= 0) {
            if (n < 0 && errno == EINTR) {
                continue;
            }
            qemu_set_fd_handler(s->fd, NULL, NULL, NULL);
            close(s->fd);
            s->fd = -1;
            return;
        }
        p += n;
        left -= n;
    }
    gpio_drive_input(s, msg.pin, msg.level);
}

void esp_gpio_drive(DeviceState *dev, unsigned pin, int level)
{
    EspGpioBlock *s = dev ? ESP_GPIO_BLOCK(dev) : the_block;

    if (s) {
        gpio_drive_input(s, pin, level);
    }
}

static void gpio_scheduled_due(void *opaque)
{
    EspGpioScheduled *due = opaque;

    gpio_drive_input(due->block, due->pin, due->level);
}

void esp_gpio_schedule(DeviceState *dev, unsigned pin, int level, int64_t at_ns)
{
    EspGpioBlock *s = dev ? ESP_GPIO_BLOCK(dev) : the_block;
    EspGpioScheduled *due;

    if (!s || pin >= GPIO_PIN_COUNT) {
        return;
    }
    due = &s->scheduled[pin];
    if (due->timer) {
        timer_del(due->timer);
    }
    if (at_ns < 0) {
        return;
    }
    if (at_ns <= qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) {
        gpio_drive_input(s, pin, level);
        return;
    }
    if (!due->timer) {
        due->block = s;
        due->pin = pin;
        due->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, gpio_scheduled_due, due);
    }
    due->level = level;
    timer_mod_ns(due->timer, at_ns);
}

static void gpio_realize(DeviceState *dev, Error **errp)
{
    EspGpioBlock *s = ESP_GPIO_BLOCK(dev);

    if (s->irq_source >= 0) {
        Object *intc = object_resolve_path_type("", s->intc_type, NULL);
        if (!intc) {
            error_setg(errp, "esp-gpio-block: no '%s' in the machine",
                       s->intc_type);
            return;
        }
        s->irq_line = qdev_get_gpio_in(DEVICE(intc), s->irq_source);
        qemu_register_reset(gpio_reset, s);
    }

    if (s->socket_path) {
        struct sockaddr_un addr;

        if (strlen(s->socket_path) >= sizeof(addr.sun_path)) {
            error_setg(errp, "esp-gpio-block: socket path too long");
            return;
        }
        s->fd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (s->fd < 0) {
            error_setg_errno(errp, errno, "esp-gpio-block: socket()");
            return;
        }
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        pstrcpy(addr.sun_path, sizeof(addr.sun_path), s->socket_path);
        if (connect(s->fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
            error_setg_errno(errp, errno, "esp-gpio-block: connect(%s)",
                             s->socket_path);
            close(s->fd);
            s->fd = -1;
            return;
        }
        qemu_set_fd_handler(s->fd, gpio_host_readable, NULL, s);
    }

    memory_region_init_io(&s->iomem_lo, OBJECT(s), &gpio_ops, s,
                          "esp.gpio.lo", R_STRAP);
    memory_region_add_subregion_overlap(get_system_memory(), s->base,
                                        &s->iomem_lo, s->priority);
    memory_region_init_io(&s->iomem_hi, OBJECT(s), &gpio_hi_ops, s,
                          "esp.gpio.hi", 0x1000 - R_IN);
    memory_region_add_subregion_overlap(get_system_memory(), s->base + R_IN,
                                        &s->iomem_hi, s->priority);
    the_block = s;
}

static Property gpio_props[] = {
    DEFINE_PROP_STRING("intc", EspGpioBlock, intc_type),
    DEFINE_PROP_STRING("socket", EspGpioBlock, socket_path),
    DEFINE_PROP_UINT64("base", EspGpioBlock, base, 0x60004000),
    DEFINE_PROP_INT32("priority", EspGpioBlock, priority, 100),
    DEFINE_PROP_INT32("irq", EspGpioBlock, irq_source, -1),
    DEFINE_PROP_END_OF_LIST(),
};

static void gpio_init(Object *obj)
{
    EspGpioBlock *s = ESP_GPIO_BLOCK(obj);

    s->intc_type = g_strdup("misc.esp32s3.intmatrix");
    s->irq_level = -1;
    s->fd = -1;
}

static void gpio_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = gpio_realize;
    dc->user_creatable = true;
    device_class_set_props(dc, gpio_props);
}

static const TypeInfo gpio_info = {
    .name = TYPE_ESP_GPIO_BLOCK,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspGpioBlock),
    .instance_init = gpio_init,
    .class_init = gpio_class_init,
};

static void gpio_register_types(void)
{
    type_register_static(&gpio_info);
}

type_init(gpio_register_types)
