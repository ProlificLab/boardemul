/*
 * esp_bus_bridge -- forward MMIO out of QEMU to an external device model.
 *
 * This file lives in the BoardEmul repository and is copied into hw/misc/ at
 * build time. It is the entire C footprint of the project (ADR 0001, ADR 0005).
 *
 * It deliberately knows nothing about any peripheral. It moves bytes across a
 * socket and does not interpret them. If a register name ever appears in this
 * file, the design has gone wrong.
 */

#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "hw/irq.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qapi/error.h"
#include "qom/object.h"
#include "qemu/main-loop.h"

#include <sys/socket.h>
#include <sys/un.h>

#define TYPE_ESP_BUS_BRIDGE "esp-bus-bridge"
OBJECT_DECLARE_SIMPLE_TYPE(EspBusBridge, ESP_BUS_BRIDGE)

#define BRIDGE_OP_READ  0u
#define BRIDGE_OP_WRITE 1u

struct EspBusBridge {
    DeviceState parent_obj;

    MemoryRegion iomem;
    char *socket_path;
    char *region_name;
    char *intc_type;
    uint64_t base;
    uint64_t size;
    int32_t priority;
    int32_t irq_source;

    int fd;
    bool broken;
    qemu_irq irq_line;
    int irq_level;

    /* Optional out-of-band interrupt channel. Levels applied through the
     * request/response path only reach the guest when it happens to access
     * *this* window, which is useless for a peripheral it is waiting on rather
     * than polling. This socket lets the model raise a line at any time. */
    char *irq_socket_path;
    int irqfd;
    DeviceState *intc_dev;
};

typedef struct QEMU_PACKED {
    uint32_t source;
    uint32_t level;
} BridgeIrqMessage;

/* Fixed-size frames, sized and ordered so both sides use one struct.unpack
 * with no framing. The reply carries the model's desired interrupt level so
 * that a completion raised by the guest's own write is delivered on the same
 * round trip -- no second channel, and no window where the guest could sleep
 * before the interrupt arrives. */
typedef struct QEMU_PACKED {
    uint8_t op;
    uint8_t size;
    uint16_t pad;
    uint32_t addr;
    uint32_t value;
} BridgeRequest;

typedef struct QEMU_PACKED {
    uint32_t value;
    uint32_t irq_level;
} BridgeResponse;

static bool bridge_write_all(EspBusBridge *s, const void *buf, size_t len)
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

static bool bridge_read_fd(int fd, void *buf, size_t len)
{
    uint8_t *p = buf;
    while (len) {
        ssize_t n = recv(fd, p, len, 0);
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

/* Runs in the main loop with the BQL already held, which is what makes calling
 * qemu_set_irq from here safe. */
static void bridge_irq_readable(void *opaque)
{
    EspBusBridge *s = opaque;
    BridgeIrqMessage msg;

    if (!bridge_read_fd(s->irqfd, &msg, sizeof(msg))) {
        qemu_set_fd_handler(s->irqfd, NULL, NULL, NULL);
        close(s->irqfd);
        s->irqfd = -1;
        return;
    }
    if (!s->intc_dev) {
        return;
    }
    qemu_set_irq(qdev_get_gpio_in(s->intc_dev, msg.source), msg.level);
}

static bool bridge_read_all(EspBusBridge *s, void *buf, size_t len)
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

/* A dead socket must not take the guest down with it: report once, then behave
 * like the unmapped region this was overlaying in the first place. */
static void bridge_fail(EspBusBridge *s, const char *what)
{
    if (!s->broken) {
        s->broken = true;
        error_report("esp-bus-bridge(%s): %s; region is inert from now on",
                     s->region_name ? s->region_name : "?", what);
    }
}

/* Safe to call directly: MMIO handlers already run under the BQL. */
static void bridge_apply_irq(EspBusBridge *s, uint32_t level)
{
    if (!s->irq_line || (int)level == s->irq_level) {
        return;
    }
    s->irq_level = level;
    qemu_set_irq(s->irq_line, s->irq_level);
}

static bool bridge_transact(EspBusBridge *s, const BridgeRequest *req,
                            BridgeResponse *rsp)
{
    if (s->broken) {
        return false;
    }
    if (!bridge_write_all(s, req, sizeof(*req))) {
        bridge_fail(s, "send failed");
        return false;
    }
    if (!bridge_read_all(s, rsp, sizeof(*rsp))) {
        bridge_fail(s, "recv failed");
        return false;
    }
    bridge_apply_irq(s, rsp->irq_level);
    return true;
}

static uint64_t bridge_mmio_read(void *opaque, hwaddr offset, unsigned size)
{
    EspBusBridge *s = opaque;
    BridgeRequest req = {
        .op = BRIDGE_OP_READ,
        .size = size,
        .pad = 0,
        .addr = (uint32_t)(s->base + offset),
        .value = 0,
    };
    BridgeResponse rsp = { 0, 0 };

    return bridge_transact(s, &req, &rsp) ? rsp.value : 0;
}

static void bridge_mmio_write(void *opaque, hwaddr offset,
                              uint64_t value, unsigned size)
{
    EspBusBridge *s = opaque;
    BridgeRequest req = {
        .op = BRIDGE_OP_WRITE,
        .size = size,
        .pad = 0,
        .addr = (uint32_t)(s->base + offset),
        .value = (uint32_t)value,
    };
    BridgeResponse rsp = { 0, 0 };

    /* Waiting for the reply keeps the guest from racing ahead of the model
     * (ADR 0005 decision 6). */
    bridge_transact(s, &req, &rsp);
}

static const MemoryRegionOps bridge_ops = {
    .read = bridge_mmio_read,
    .write = bridge_mmio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

static void bridge_realize(DeviceState *dev, Error **errp)
{
    EspBusBridge *s = ESP_BUS_BRIDGE(dev);
    struct sockaddr_un addr;

    if (!s->socket_path) {
        error_setg(errp, "esp-bus-bridge: 'socket' property is required");
        return;
    }
    if (!s->size) {
        error_setg(errp, "esp-bus-bridge: 'size' property is required");
        return;
    }
    if (strlen(s->socket_path) >= sizeof(addr.sun_path)) {
        error_setg(errp, "esp-bus-bridge: socket path too long");
        return;
    }

    /* Looked up by type name rather than by including the machine's header,
     * so this file stays independent of the SoC it is plugged into. */
    if (s->irq_source >= 0 || s->irq_socket_path) {
        Object *intc = object_resolve_path_type("", s->intc_type, NULL);
        if (!intc) {
            error_setg(errp, "esp-bus-bridge: no '%s' in the machine",
                       s->intc_type);
            return;
        }
        s->intc_dev = DEVICE(intc);
    }
    if (s->irq_source >= 0) {
        Object *intc = OBJECT(s->intc_dev);
        s->irq_line = qdev_get_gpio_in(DEVICE(intc), s->irq_source);
        if (!s->irq_line) {
            error_setg(errp, "esp-bus-bridge: '%s' has no input %d",
                       s->intc_type, s->irq_source);
            return;
        }
    }

    s->fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (s->fd < 0) {
        error_setg_errno(errp, errno, "esp-bus-bridge: socket()");
        return;
    }
    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    pstrcpy(addr.sun_path, sizeof(addr.sun_path), s->socket_path);
    if (connect(s->fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        error_setg_errno(errp, errno, "esp-bus-bridge: connect(%s)",
                         s->socket_path);
        close(s->fd);
        s->fd = -1;
        return;
    }

    if (s->irq_socket_path) {
        struct sockaddr_un iaddr;

        if (strlen(s->irq_socket_path) >= sizeof(iaddr.sun_path)) {
            error_setg(errp, "esp-bus-bridge: irq socket path too long");
            return;
        }
        s->irqfd = socket(AF_UNIX, SOCK_STREAM, 0);
        if (s->irqfd < 0) {
            error_setg_errno(errp, errno, "esp-bus-bridge: irq socket()");
            return;
        }
        memset(&iaddr, 0, sizeof(iaddr));
        iaddr.sun_family = AF_UNIX;
        pstrcpy(iaddr.sun_path, sizeof(iaddr.sun_path), s->irq_socket_path);
        if (connect(s->irqfd, (struct sockaddr *)&iaddr, sizeof(iaddr)) < 0) {
            error_setg_errno(errp, errno, "esp-bus-bridge: irq connect(%s)",
                             s->irq_socket_path);
            close(s->irqfd);
            s->irqfd = -1;
            return;
        }
        qemu_set_fd_handler(s->irqfd, bridge_irq_readable, NULL, s);
    }

    memory_region_init_io(&s->iomem, OBJECT(s), &bridge_ops, s,
                          s->region_name ? s->region_name : TYPE_ESP_BUS_BRIDGE,
                          s->size);
    /* Overlap rather than replace: the machine's catch-all region stays
     * underneath, so anything outside our window is unaffected. */
    memory_region_add_subregion_overlap(get_system_memory(), s->base,
                                        &s->iomem, s->priority);
}

static Property bridge_props[] = {
    DEFINE_PROP_STRING("socket", EspBusBridge, socket_path),
    DEFINE_PROP_STRING("name", EspBusBridge, region_name),
    DEFINE_PROP_STRING("intc", EspBusBridge, intc_type),
    DEFINE_PROP_STRING("irqsock", EspBusBridge, irq_socket_path),
    DEFINE_PROP_UINT64("base", EspBusBridge, base, 0),
    DEFINE_PROP_UINT64("size", EspBusBridge, size, 0),
    DEFINE_PROP_INT32("priority", EspBusBridge, priority, 100),
    DEFINE_PROP_INT32("irq", EspBusBridge, irq_source, -1),
    DEFINE_PROP_END_OF_LIST(),
};

static void bridge_init(Object *obj)
{
    EspBusBridge *s = ESP_BUS_BRIDGE(obj);

    s->intc_type = g_strdup("misc.esp32s3.intmatrix");
    s->irq_level = -1;
    s->fd = -1;
    s->irqfd = -1;
}

static void bridge_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = bridge_realize;
    dc->user_creatable = true;
    device_class_set_props(dc, bridge_props);
}

static const TypeInfo bridge_info = {
    .name = TYPE_ESP_BUS_BRIDGE,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspBusBridge),
    .instance_init = bridge_init,
    .class_init = bridge_class_init,
};

static void bridge_register_types(void)
{
    type_register_static(&bridge_info);
}

type_init(bridge_register_types)
