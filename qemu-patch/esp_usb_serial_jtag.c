/*
 * ESP32-S3 USB Serial/JTAG controller: the serial side, with a host attached.
 *
 * ADR 0100. The fork maps a placeholder at this address that reads 0 and drops
 * every write, so a firmware whose Serial is this port (Arduino's HWCDC) was
 * printing into nothing. This is the port as the IDF's register header
 * (soc/esp32s3/include/soc/usb_serial_jtag_reg.h) and low-level driver
 * (hal/esp32s3/include/hal/usb_serial_jtag_ll.h) describe it, with a host on
 * the other end that is plugged in and listening.
 *
 * Device to host (IN): bytes are written to EP1 while SERIAL_IN_EP_DATA_FREE
 * is set, and WR_DONE -- or the FIFO filling -- hands the packet to the host,
 * which takes it after
 * the time a full-speed bus needs to carry it, and only then is the FIFO free
 * again. SERIAL_IN_EMPTY is raised for as long as the IN FIFO is empty: Arduino
 * 2.0.14's HWCDC::begin() clears every interrupt and then enables that one, and
 * the interrupt that follows is how it learns that a host is there.
 *
 * Host to device (OUT): what the chardev delivers, in packets of up to 64
 * bytes, one at a time. Each raises SERIAL_OUT_RECV_PKT and is read back
 * through EP1 while SERIAL_OUT_EP_DATA_AVAIL is set.
 *
 * The JTAG side is not modelled: its FIFO reports empty, as at reset.
 */

#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "qemu/timer.h"
#include "qemu/main-loop.h"
#include "qemu/log.h"
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

#define TYPE_ESP_USB_SERIAL_JTAG "esp-usb-serial-jtag"
OBJECT_DECLARE_SIMPLE_TYPE(EspUsbSerialJtag, ESP_USB_SERIAL_JTAG)

/* Offsets and fields, from usb_serial_jtag_reg.h. */
#define R_EP1           0x00
#define R_EP1_CONF      0x04
#define   WR_DONE                   (1u << 0)
#define   SERIAL_IN_EP_DATA_FREE    (1u << 1)
#define   SERIAL_OUT_EP_DATA_AVAIL  (1u << 2)
#define R_INT_RAW       0x08
#define R_INT_ST        0x0C
#define R_INT_ENA       0x10
#define R_INT_CLR       0x14
#define   INT_SOF                   (1u << 1)
#define   INT_SERIAL_OUT_RECV_PKT   (1u << 2)
#define   INT_SERIAL_IN_EMPTY       (1u << 3)
#define   INT_ALL                   0xFFFFu
#define R_CONF0         0x18
#define R_TEST          0x1C
#define R_JFIFO_ST      0x20
#define   JFIFO_RESET_VALUE         ((1u << 6) | (1u << 2))  /* both empty */
#define R_FRAM_NUM      0x24
#define R_IN_EP0_ST     0x28
#define R_IN_EP1_ST     0x2C
#define R_IN_EP2_ST     0x30
#define R_IN_EP3_ST     0x34
#define R_OUT_EP0_ST    0x38
#define R_OUT_EP1_ST    0x3C
#define R_OUT_EP2_ST    0x40
#define R_MISC_CONF     0x44
#define R_MEM_CONF      0x48
#define R_DATE          0x80

#define CONF0_RESET     ((1u << 14) | (1u << 9))   /* pad enabled, D+ pulled up */
#define MEM_CONF_RESET  (1u << 1)                  /* memory clock enabled */
#define DATE_RESET      0x2101200u
#define EP_STATE_IDLE   1u                         /* IN_EPn_STATE at reset */

#define FIFO_BYTES      64
#define REGS_SIZE       0x1000
/* Full speed is 12 Mbit/s: one byte every 667 ns on the wire. */
#define NS_PER_BYTE     667
#define NS_PER_FRAME    1000000                    /* a SOF each millisecond */

struct EspUsbSerialJtag {
    DeviceState parent_obj;

    MemoryRegion iomem;
    CharBackend chr;
    uint64_t base;
    int32_t priority;
    int32_t irq_source;
    char *intc_type;

    qemu_irq irq_line;
    int irq_level;
    QEMUBH *irq_bh;
    QEMUTimer *host_timer;      /* the host taking a flushed IN packet */
    QEMUTimer *sof_timer;

    uint32_t raw, ena, conf0, test, misc_conf, mem_conf, date;

    uint8_t in[FIFO_BYTES];
    unsigned in_len;
    bool in_flushed;            /* WR_DONE seen; the host has it next */

    uint8_t out[FIFO_BYTES];
    unsigned out_len, out_pos;

    uint64_t sent, received;
};

/* -- the line ------------------------------------------------------------ */

static void usj_irq_bh(void *opaque)
{
    EspUsbSerialJtag *s = opaque;
    int level = (s->raw & s->ena) ? 1 : 0;

    if (!s->irq_line || level == s->irq_level) {
        return;
    }
    s->irq_level = level;
    qemu_set_irq(s->irq_line, level);
}

/* Deferred as the other devices' are (esp_ble_core.c): raising the line
 * inside the guest's own write would deliver it before the setter is done. */
static void usj_update(EspUsbSerialJtag *s)
{
    /* Level, not latched: an empty IN FIFO keeps asking to be filled. */
    if (!s->in_len && !s->in_flushed) {
        s->raw |= INT_SERIAL_IN_EMPTY;
    }
    if (s->irq_bh) {
        qemu_bh_schedule(s->irq_bh);
    }
}

static void usj_arm_sof(EspUsbSerialJtag *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (s->ena & INT_SOF) {
        timer_mod_ns(s->sof_timer, (now / NS_PER_FRAME + 1) * NS_PER_FRAME);
    } else {
        timer_del(s->sof_timer);
    }
}

static void usj_sof(void *opaque)
{
    EspUsbSerialJtag *s = opaque;

    s->raw |= INT_SOF;
    usj_update(s);
    usj_arm_sof(s);
}

/* -- device to host ------------------------------------------------------ */

static void usj_host_takes(void *opaque)
{
    EspUsbSerialJtag *s = opaque;

    if (!s->in_flushed) {
        return;
    }
    if (s->in_len && qemu_chr_fe_backend_connected(&s->chr)) {
        qemu_chr_fe_write_all(&s->chr, s->in, s->in_len);
    }
    s->sent += s->in_len;
    s->in_len = 0;
    s->in_flushed = false;
    usj_update(s);
}

static void usj_flush(EspUsbSerialJtag *s)
{
    if (s->in_flushed) {
        return;
    }
    s->in_flushed = true;
    timer_mod_ns(s->host_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                                + (int64_t)(s->in_len ? s->in_len : 1) * NS_PER_BYTE);
}

/* -- host to device ------------------------------------------------------ */

static int usj_can_receive(void *opaque)
{
    EspUsbSerialJtag *s = opaque;

    return s->out_pos < s->out_len ? 0 : FIFO_BYTES;
}

static void usj_receive(void *opaque, const uint8_t *buf, int size)
{
    EspUsbSerialJtag *s = opaque;

    if (size > FIFO_BYTES) {
        size = FIFO_BYTES;
    }
    memcpy(s->out, buf, size);
    s->out_len = size;
    s->out_pos = 0;
    s->received += size;
    s->raw |= INT_SERIAL_OUT_RECV_PKT;
    usj_update(s);
}

/* -- registers ----------------------------------------------------------- */

static uint32_t usj_ep1_conf(EspUsbSerialJtag *s)
{
    uint32_t v = 0;

    if (!s->in_flushed && s->in_len < FIFO_BYTES) {
        v |= SERIAL_IN_EP_DATA_FREE;
    }
    if (s->out_pos < s->out_len) {
        v |= SERIAL_OUT_EP_DATA_AVAIL;
    }
    return v;
}

static uint64_t usj_read(void *opaque, hwaddr offset, unsigned size)
{
    EspUsbSerialJtag *s = opaque;
    uint32_t v = 0;

    switch (offset) {
    case R_EP1:
        if (s->out_pos < s->out_len) {
            v = s->out[s->out_pos++];
            if (s->out_pos == s->out_len) {
                s->out_len = s->out_pos = 0;
                qemu_chr_fe_accept_input(&s->chr);
            }
        }
        break;
    case R_EP1_CONF:
        v = usj_ep1_conf(s);
        break;
    case R_INT_RAW:
        v = s->raw;
        break;
    case R_INT_ST:
        v = s->raw & s->ena;
        break;
    case R_INT_ENA:
        v = s->ena;
        break;
    case R_CONF0:
        v = s->conf0;
        break;
    case R_TEST:
        v = s->test;
        break;
    case R_JFIFO_ST:
        v = JFIFO_RESET_VALUE;
        break;
    case R_FRAM_NUM:
        v = (uint32_t)(qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / NS_PER_FRAME) & 0x7FF;
        break;
    case R_IN_EP0_ST:
    case R_IN_EP2_ST:
    case R_IN_EP3_ST:
        v = EP_STATE_IDLE;
        break;
    case R_IN_EP1_ST:
        /* Write address in 8..2, read address in 15..9, both FIFO positions. */
        v = EP_STATE_IDLE | ((s->in_len & 0x7F) << 2);
        break;
    case R_OUT_EP1_ST:
        v = (((s->out_len - s->out_pos) & 0x7F) << 16)
            | ((s->out_pos & 0x7F) << 9) | ((s->out_len & 0x7F) << 2);
        break;
    case R_MISC_CONF:
        v = s->misc_conf;
        break;
    case R_MEM_CONF:
        v = s->mem_conf;
        break;
    case R_DATE:
        v = s->date;
        break;
    default:
        break;
    }
    return v;
}

static void usj_write(void *opaque, hwaddr offset, uint64_t value, unsigned size)
{
    EspUsbSerialJtag *s = opaque;
    uint32_t v = (uint32_t)value;

    switch (offset) {
    case R_EP1:
        if (!s->in_flushed && s->in_len < FIFO_BYTES) {
            s->in[s->in_len++] = v & 0xFF;
            /* A full FIFO goes to the host without WR_DONE. The ROM's console
             * (0x40049068) depends on it: it fills all 64 bytes and then waits
             * for room, and never writes WR_DONE in between. */
            if (s->in_len == FIFO_BYTES) {
                usj_flush(s);
            }
        }
        break;
    case R_EP1_CONF:
        if (v & WR_DONE) {
            usj_flush(s);
        }
        break;
    case R_INT_ENA:
        s->ena = v & INT_ALL;
        usj_arm_sof(s);
        break;
    case R_INT_CLR:
        s->raw &= ~(v & INT_ALL);
        break;
    case R_CONF0:
        s->conf0 = v;
        break;
    case R_TEST:
        s->test = v & 0xF;
        break;
    case R_MISC_CONF:
        s->misc_conf = v & 1;
        break;
    case R_MEM_CONF:
        s->mem_conf = v & 3;
        break;
    case R_DATE:
        s->date = v;
        break;
    default:
        break;
    }
    usj_update(s);
}

static const MemoryRegionOps usj_ops = {
    .read = usj_read,
    .write = usj_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

/* -- the device ---------------------------------------------------------- */

static void usj_reset(void *opaque)
{
    EspUsbSerialJtag *s = opaque;

    timer_del(s->host_timer);
    timer_del(s->sof_timer);
    s->raw = 0;
    s->ena = 0;
    s->conf0 = CONF0_RESET;
    s->test = 0;
    s->misc_conf = 0;
    s->mem_conf = MEM_CONF_RESET;
    s->date = DATE_RESET;
    s->in_len = 0;
    s->in_flushed = false;
    s->out_len = s->out_pos = 0;
    usj_update(s);
}

static void usj_realize(DeviceState *dev, Error **errp)
{
    EspUsbSerialJtag *s = ESP_USB_SERIAL_JTAG(dev);

    if (s->irq_source >= 0) {
        Object *intc = object_resolve_path_type("", s->intc_type, NULL);
        if (!intc) {
            error_setg(errp, "esp-usb-serial-jtag: no '%s' in the machine",
                       s->intc_type);
            return;
        }
        s->irq_line = qdev_get_gpio_in(DEVICE(intc), s->irq_source);
        s->irq_bh = qemu_bh_new(usj_irq_bh, s);
    }
    s->host_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, usj_host_takes, s);
    s->sof_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, usj_sof, s);
    qemu_chr_fe_set_handlers(&s->chr, usj_can_receive, usj_receive,
                             NULL, NULL, s, NULL, true);

    memory_region_init_io(&s->iomem, OBJECT(s), &usj_ops, s,
                          TYPE_ESP_USB_SERIAL_JTAG, REGS_SIZE);
    memory_region_add_subregion_overlap(get_system_memory(), s->base, &s->iomem,
                                        s->priority);
    /* A plain device is not in the tree the reset phases walk, so it asks to
     * be reset with the machine, as the bus bridge does (ADR 0035). */
    qemu_register_reset(usj_reset, s);
    usj_reset(s);
}

static Property usj_props[] = {
    DEFINE_PROP_CHR("chardev", EspUsbSerialJtag, chr),
    DEFINE_PROP_STRING("intc", EspUsbSerialJtag, intc_type),
    DEFINE_PROP_UINT64("base", EspUsbSerialJtag, base, 0x60038000),
    DEFINE_PROP_INT32("priority", EspUsbSerialJtag, priority, 100),
    DEFINE_PROP_INT32("irq", EspUsbSerialJtag, irq_source, 96),
    DEFINE_PROP_END_OF_LIST(),
};

static void usj_instance_init(Object *obj)
{
    EspUsbSerialJtag *s = ESP_USB_SERIAL_JTAG(obj);

    s->intc_type = g_strdup("misc.esp32s3.intmatrix");
    s->irq_level = 0;
}

static void usj_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = usj_realize;
    device_class_set_props(dc, usj_props);
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
}

static const TypeInfo usj_info = {
    .name = TYPE_ESP_USB_SERIAL_JTAG,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspUsbSerialJtag),
    .instance_init = usj_instance_init,
    .class_init = usj_class_init,
};

static void usj_register_types(void)
{
    type_register_static(&usj_info);
}

type_init(usj_register_types)
