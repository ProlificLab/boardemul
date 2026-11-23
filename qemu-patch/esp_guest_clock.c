/*
 * SPDX-FileCopyrightText: 2026 Prolific Lab
 * SPDX-License-Identifier: GPL-2.0-or-later
 */
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
 *
 * Given a quantum, the tick is a barrier instead (ADR 0113): every quantum it
 * sends the time and waits for an answer before the guest runs on. Under
 * -icount the vCPU runs a due virtual timer itself and its budget never passes
 * the next deadline, so waiting here stops the guest's time at an exact
 * instruction -- which is what lets boards on one air keep one time. The
 * answer lists lines to move:
 *
 *     u32 count, then count times:
 *     u32 pin   u8 level (0, 1, or 0xFF to leave it)   u8 pad[3]   u64 rise_at_ns
 *
 * applied with esp_gpio_drive() and esp_gpio_schedule() (ADR 0112).
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
#include "hw/misc/esp_board_glue.h"
#include "hw/core/cpu.h"
#include "sysemu/cpu-timers.h"

#include <sys/socket.h>
#include <sys/un.h>

#define TYPE_ESP_GUEST_CLOCK "esp-guest-clock"
OBJECT_DECLARE_SIMPLE_TYPE(EspGuestClock, ESP_GUEST_CLOCK)

struct EspGuestClock {
    DeviceState parent_obj;

    char *socket_path;
    uint32_t period_ms;
    uint64_t quantum_ns;        /* nonzero: a barrier every quantum (ADR 0113) */
    int fd;
    QEMUTimer *timer;
    uint64_t ticks, skipped;
};

typedef struct QEMU_PACKED {
    uint32_t pin;
    uint8_t level;
    uint8_t pad[3];
    uint64_t rise_at_ns;
} EspClockLine;

#define LINE_LEFT 0xFF
#define LINES_MAX 64

static bool clock_read_all(int fd, void *buf, size_t len)
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

static bool clock_send_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = buf;

    while (len) {
        ssize_t n = send(fd, p, len, MSG_NOSIGNAL);
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

/* One barrier: say the time, wait to be let go, move the lines it names. */
static bool clock_barrier(EspGuestClock *s, int64_t now)
{
    uint8_t frame[8];
    uint32_t count;

    stq_le_p(frame, (uint64_t)now);
    if (!clock_send_all(s->fd, frame, sizeof(frame))
        || !clock_read_all(s->fd, &count, sizeof(count))) {
        return false;
    }
    count = le32_to_cpu(count);
    for (uint32_t i = 0; i < count; i++) {
        EspClockLine line;

        if (!clock_read_all(s->fd, &line, sizeof(line))) {
            return false;
        }
        if (i >= LINES_MAX || line.level == LINE_LEFT) {
            continue;
        }
        esp_gpio_drive(NULL, le32_to_cpu(line.pin), line.level & 1);
        esp_gpio_schedule(NULL, le32_to_cpu(line.pin), 1,
                          line.rise_at_ns ? (int64_t)le64_to_cpu(line.rise_at_ns)
                                          : -1);
    }
    return true;
}

static void clock_tick(void *opaque)
{
    EspGuestClock *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint8_t frame[8];
    ssize_t n;

    if (s->fd < 0) {
        return;
    }
    if (s->quantum_ns) {
        if (!clock_barrier(s, now)) {
            warn_report("esp-guest-clock: the barrier's other end has gone; "
                        "the clock stops");
            close(s->fd);
            s->fd = -1;
            return;
        }
        s->ticks++;
        timer_mod_ns(s->timer, now + (int64_t)s->quantum_ns);
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
    DEFINE_PROP_UINT64("quantum_ns", EspGuestClock, quantum_ns, 0),
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

/*
 * How long one core runs before the other gets its turn (ADR 0161).
 *
 * Under one translation thread (ADR 0041) QEMU runs the two cores in turn and
 * moves to the next one when the running core halts or every 100 ms of
 * virtual time (TCG_KICK_PERIOD). The S3's cores hand work to each other
 * through spinlocks and flags in shared memory, and on silicon the wait for
 * the other core lasts as long as the other core takes, microseconds. Here
 * the other core is not running at all, so the waiting core spins out the
 * rest of its 100 ms: a Meshtastic boot spent nine emulated instructions in
 * ten in two such loops.
 *
 * This shortens the turn. On a virtual-clock timer every period it makes the
 * running core leave its translated code, as QEMU's own kick does, and the
 * next core runs. Nothing changes under several translation threads, where
 * the cores already run at once, or under -icount, whose budgets end each
 * turn at the next timer deadline already and whose interleaving the
 * deterministic runs depend on (ADR 0115).
 */

#define TYPE_ESP_CORE_SLICE "esp-core-slice"
OBJECT_DECLARE_SIMPLE_TYPE(EspCoreSlice, ESP_CORE_SLICE)

struct EspCoreSlice {
    DeviceState parent_obj;

    uint32_t period_us;
    QEMUTimer *timer;
    uint64_t turns;
};

static void slice_tick(void *opaque)
{
    EspCoreSlice *s = opaque;
    CPUState *cs;

    CPU_FOREACH(cs) {
        if (qatomic_read(&cs->running)) {
            cpu_exit(cs);
            s->turns++;
        }
    }
    timer_mod_ns(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                 + (int64_t)s->period_us * SCALE_US);
}

static void slice_realize(DeviceState *dev, Error **errp)
{
    EspCoreSlice *s = ESP_CORE_SLICE(dev);

    if (!s->period_us || qemu_tcg_mttcg_enabled() || icount_enabled()) {
        return;
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, slice_tick, s);
    timer_mod_ns(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)
                 + (int64_t)s->period_us * SCALE_US);
}

static Property slice_props[] = {
    DEFINE_PROP_UINT32("period_us", EspCoreSlice, period_us, 1000),
    DEFINE_PROP_END_OF_LIST(),
};

static void slice_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = slice_realize;
    device_class_set_props(dc, slice_props);
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
}

static const TypeInfo slice_info = {
    .name = TYPE_ESP_CORE_SLICE,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspCoreSlice),
    .class_init = slice_class_init,
};

static void clock_register_types(void)
{
    type_register_static(&clock_info);
    type_register_static(&slice_info);
}

type_init(clock_register_types)
