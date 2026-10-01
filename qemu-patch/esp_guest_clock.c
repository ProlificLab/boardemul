/*
 * The guest's clock, for the board's parts in Python (ADR 0104).
 *
 * No registers: on a virtual-clock timer this writes the current virtual time,
 * eight bytes little-endian in nanoseconds, to a Unix socket the Python side
 * listens on. That is the board's own time -- slower than the host's when the
 * guest is slow, and the instruction count under -icount -- which a part that
 * streams on its own (a satellite receiver's fix a second) has to keep.
 *
 * A full socket skips the tick rather than block the guest; a closed one
 * stops the clock.
 */

#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "qemu/timer.h"
#include "qemu/cutils.h"
#include "qemu/bswap.h"
#include "qemu/main-loop.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "qapi/error.h"
#include "qom/object.h"

#include <sys/socket.h>
#include <sys/un.h>

#define TYPE_ESP_GUEST_CLOCK "esp-guest-clock"
OBJECT_DECLARE_SIMPLE_TYPE(EspGuestClock, ESP_GUEST_CLOCK)

struct EspGuestClock {
    DeviceState parent_obj;

    char *socket_path;
    uint32_t period_ms;
    int fd;
    QEMUTimer *timer;
    uint64_t ticks, skipped;
};

static void clock_tick(void *opaque)
{
    EspGuestClock *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint8_t frame[8];
    ssize_t n;

    if (s->fd < 0) {
        return;
    }
    stq_le_p(frame, (uint64_t)now);
    n = send(s->fd, frame, sizeof(frame), MSG_DONTWAIT | MSG_NOSIGNAL);
    if (n == sizeof(frame)) {
        s->ticks++;
    } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        s->skipped++;
    } else {
        warn_report("esp-guest-clock: the other end has gone; the clock stops");
        close(s->fd);
        s->fd = -1;
        return;
    }
    timer_mod_ns(s->timer, now + (int64_t)s->period_ms * SCALE_MS);
}

static void clock_realize(DeviceState *dev, Error **errp)
{
    EspGuestClock *s = ESP_GUEST_CLOCK(dev);
    struct sockaddr_un addr = { 0 };

    s->fd = -1;
    if (!s->socket_path) {
        error_setg(errp, "esp-guest-clock: needs socket=PATH");
        return;
    }
    if (strlen(s->socket_path) >= sizeof(addr.sun_path)) {
        error_setg(errp, "esp-guest-clock: socket path too long");
        return;
    }
    s->fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (s->fd < 0) {
        error_setg_errno(errp, errno, "esp-guest-clock: socket()");
        return;
    }
    addr.sun_family = AF_UNIX;
    pstrcpy(addr.sun_path, sizeof(addr.sun_path), s->socket_path);
    if (connect(s->fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        error_setg_errno(errp, errno, "esp-guest-clock: connect(%s)", s->socket_path);
        close(s->fd);
        s->fd = -1;
        return;
    }
    if (!s->period_ms) {
        s->period_ms = 10;
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, clock_tick, s);
    timer_mod_ns(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

static Property clock_props[] = {
    DEFINE_PROP_STRING("socket", EspGuestClock, socket_path),
    DEFINE_PROP_UINT32("period_ms", EspGuestClock, period_ms, 10),
    DEFINE_PROP_END_OF_LIST(),
};

static void clock_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = clock_realize;
    device_class_set_props(dc, clock_props);
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
}

static const TypeInfo clock_info = {
    .name = TYPE_ESP_GUEST_CLOCK,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspGuestClock),
    .class_init = clock_class_init,
};

static void clock_register_types(void)
{
    type_register_static(&clock_info);
}

type_init(clock_register_types)
