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
#include "qemu/log.h"
#include "hw/core/cpu.h"
#include "sysemu/reset.h"

#include <sys/socket.h>
#include <sys/un.h>
#include <signal.h>
#include <execinfo.h>
#include <ucontext.h>

#define TYPE_ESP_BUS_BRIDGE "esp-bus-bridge"
OBJECT_DECLARE_SIMPLE_TYPE(EspBusBridge, ESP_BUS_BRIDGE)

#define BRIDGE_OP_READ  0u
#define BRIDGE_OP_WRITE 1u
/* The machine reset. Not acknowledged, like a write: it carries no value and
 * nothing waits on it. */
#define BRIDGE_OP_RESET 2u
/* A read this device answered from its shadow, with the value it gave: posted
 * so the model counts it and keeps it in its tail, and nothing waits (ADR
 * 0176). */
#define BRIDGE_OP_READ_SEEN 3u

struct EspBusBridge {
    DeviceState parent_obj;

    MemoryRegion iomem;
    char *socket_path;
    char *region_name;
    char *intc_type;
    uint64_t base;
    uint64_t size;
    int32_t priority;

    int fd;
    bool broken;

    /* Optional out-of-band interrupt channel. Levels applied through the
     * request/response path only reach the guest when it happens to access
     * *this* window, which is useless for a peripheral it is waiting on rather
     * than polling. This socket lets the model raise a line at any time. */
    char *irq_socket_path;
    int irqfd;
    DeviceState *intc_dev;

    /* A trace window, read from here (ADR 0176): what the Python model
     * would answer, which is the last value written at that offset, ORed
     * with the gate bits for that address ("addr:mask;..."), cleared at
     * reset. The model still hears of every access. */
    bool shadow;
    char *gates_spec;
    GHashTable *regs;       /* offset -> last value written */
    GHashTable *gates;      /* absolute address -> bits forced high on read */
};

typedef struct QEMU_PACKED {
    uint32_t source;
    uint32_t level;
} BridgeIrqMessage;

/* Fixed-size frames, sized and ordered so both sides use one struct.unpack
 * with no framing.
 *
 * Writes are not acknowledged (ADR 0011 decision 6). The socket is FIFO, so a
 * read issued after a write still observes it, and not blocking on writes
 * removes roughly half the round trips. Interrupts therefore never ride the
 * reply -- they all arrive on the out-of-band channel. */
typedef struct QEMU_PACKED {
    uint8_t op;
    uint8_t size;
    uint16_t pad;
    uint32_t addr;
    uint32_t value;
} BridgeRequest;

typedef struct QEMU_PACKED {
    uint32_t value;
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
    return true;
}

/* Fire and forget: no reply is expected, so the vCPU does not stall. */
static bool bridge_post(EspBusBridge *s, const BridgeRequest *req)
{
    if (s->broken) {
        return false;
    }
    if (!bridge_write_all(s, req, sizeof(*req))) {
        bridge_fail(s, "send failed");
        return false;
    }
    return true;
}

static bool bridge_pc_watched(uint32_t addr)
{
    static bool parsed;
    static uint32_t lo[16], hi[16];
    static int count;

    if (!parsed) {
        parsed = true;
        const char *env = getenv("BOARDEMUL_PC_WATCH");
        if (env) {
            char *dup = g_strdup(env);
            for (char *tok = strtok(dup, ","); tok && count < 16;
                 tok = strtok(NULL, ",")) {
                char *dash = strchr(tok, '-');
                if (dash) {
                    *dash = 0;
                    lo[count] = (uint32_t)strtoul(tok, NULL, 0);
                    hi[count] = (uint32_t)strtoul(dash + 1, NULL, 0);
                } else {
                    lo[count] = hi[count] = (uint32_t)strtoul(tok, NULL, 0);
                }
                count++;
            }
            g_free(dup);
        }
    }
    for (int i = 0; i < count; i++) {
        if (lo[i] <= addr && addr <= hi[i]) {
            return true;
        }
    }
    return false;
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
    BridgeResponse rsp = { 0 };

    /* Investigation hatch, off unless BOARDEMUL_PC_WATCH names addresses or
     * lo-hi ranges: log the guest PC on every read of them. Knowing which code
     * polls a register is what turns "the firmware is stuck" into a loop that
     * can be disassembled and answered -- it is how each radio bring-up gate
     * was found (ADR 0019). No register is named here; the addresses come from
     * the environment, as the debug flags do in run.py. */
    if (current_cpu && bridge_pc_watched(req.addr)) {
        CPUClass *cc = CPU_GET_CLASS(current_cpu);
        qemu_log_mask(LOG_UNIMP, "PCWATCH addr=0x%08x pc=0x%08" PRIx64 " cpu=%d\n",
                      req.addr, cc->get_pc(current_cpu), current_cpu->cpu_index);
    }

    if (s->shadow) {
        req.op = BRIDGE_OP_READ_SEEN;
        req.value = GPOINTER_TO_UINT(g_hash_table_lookup(s->regs,
                                                         GUINT_TO_POINTER(offset)))
                    | GPOINTER_TO_UINT(g_hash_table_lookup(s->gates,
                                                           GUINT_TO_POINTER(req.addr)));
        bridge_post(s, &req);
        return req.value;
    }
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

    /* The same hatch for writes, with the value: a timer being armed or a
     * mask being set is a write, and which code did it matters as much as
     * which code polls the result. */
    if (current_cpu && bridge_pc_watched(req.addr)) {
        CPUClass *cc = CPU_GET_CLASS(current_cpu);
        qemu_log_mask(LOG_UNIMP, "PCWATCH wr addr=0x%08x value=0x%08x pc=0x%08" PRIx64 " cpu=%d\n",
                      req.addr, req.value, cc->get_pc(current_cpu), current_cpu->cpu_index);
    }
    if (s->shadow) {
        g_hash_table_insert(s->regs, GUINT_TO_POINTER(offset),
                            GUINT_TO_POINTER(req.value));
    }
    bridge_post(s, &req);
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

/* A reset resets the peripherals inside the machine, and the ones modelled
 * outside it have to hear about it too. Otherwise a guest that reboots meets
 * registers still holding what it left there before, which is a difference
 * between its first boot and every one after it.
 *
 * Registered by hand rather than through the resettable phases: this device
 * has no parent bus, so it is not in the tree those walk, and the phase hook
 * is simply never called.
 */
static void bridge_reset(void *opaque)
{
    EspBusBridge *s = opaque;
    BridgeRequest req = {
        .op = BRIDGE_OP_RESET,
        .size = 0,
        .pad = 0,
        .addr = (uint32_t)s->base,
        .value = 0,
    };

    if (s->shadow) {
        g_hash_table_remove_all(s->regs);
    }
    if (s->fd >= 0 && !s->broken) {
        bridge_post(s, &req);
    }
}

/* "addr:mask;addr:mask", numbers in any base strtoul reads. Not commas:
 * -device splits its properties on those. */
static bool bridge_parse_gates(EspBusBridge *s, Error **errp)
{
    if (!s->gates_spec || !*s->gates_spec) {
        return true;
    }
    char **items = g_strsplit(s->gates_spec, ";", -1);
    bool ok = true;
    for (char **it = items; *it && ok; it++) {
        char *colon = strchr(*it, ':');
        if (!colon) {
            error_setg(errp, "esp-bus-bridge: gate '%s' is not addr:mask", *it);
            ok = false;
            break;
        }
        *colon = 0;
        uint32_t addr = (uint32_t)strtoul(*it, NULL, 0);
        uint32_t mask = (uint32_t)strtoul(colon + 1, NULL, 0);
        g_hash_table_insert(s->gates, GUINT_TO_POINTER(addr), GUINT_TO_POINTER(mask));
    }
    g_strfreev(items);
    return ok;
}


/* Investigation hatch, off unless BOARDEMUL_CATCH_SEGV is set: print this
 * process's own stack when it faults.
 *
 * The crash this is for happens only with two translation threads on a busy
 * machine (ADR 0041). It does not survive being watched: under gdb the timing
 * shifts and it never fires, and core dumps on this host go to a handler that
 * cannot be redirected. Reporting from inside the process costs nothing until
 * the fault, and leaves the timing alone.
 */
static void bridge_report_fault(int sig, siginfo_t *info, void *context)
{
    void *frames[48];
    char head[256];
    void *at = NULL;

#if defined(__x86_64__) && defined(REG_RIP)
    /* Where it actually faulted. The stack below is only the signal
     * frame and whatever libc can unwind; generated code has no frame
     * pointers, so this is the one address worth having. */
    if (context) {
        at = (void *)((ucontext_t *)context)->uc_mcontext.gregs[REG_RIP];
    }
#endif
    int depth = backtrace(frames, ARRAY_SIZE(frames));
    int len = snprintf(head, sizeof(head),
                       "%s=== signal %d at address %p, faulting pc %p ===%s",
                       "\n", sig, info ? info->si_addr : NULL, at, "\n");

    if (write(STDERR_FILENO, head, len) < 0) {
        /* Nothing useful to do about a failed write while crashing. */
    }
    backtrace_symbols_fd(frames, depth, STDERR_FILENO);
    _exit(128 + sig);
}

static void bridge_catch_faults(void)
{
    static bool installed;
    struct sigaction action;

    if (installed || !getenv("BOARDEMUL_CATCH_SEGV")) {
        return;
    }
    installed = true;
    memset(&action, 0, sizeof(action));
    action.sa_sigaction = bridge_report_fault;
    action.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &action, NULL);
    sigaction(SIGBUS, &action, NULL);
    sigaction(SIGABRT, &action, NULL);
}

static void bridge_realize(DeviceState *dev, Error **errp)
{
    bridge_catch_faults();
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
    if (!bridge_parse_gates(s, errp)) {
        return;
    }

    /* Looked up by type name rather than by including the machine's header,
     * so this file stays independent of the SoC it is plugged into. */
    if (s->irq_socket_path) {
        Object *intc = object_resolve_path_type("", s->intc_type, NULL);
        if (!intc) {
            error_setg(errp, "esp-bus-bridge: no '%s' in the machine",
                       s->intc_type);
            return;
        }
        s->intc_dev = DEVICE(intc);
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
    qemu_register_reset(bridge_reset, s);
}

static Property bridge_props[] = {
    DEFINE_PROP_STRING("socket", EspBusBridge, socket_path),
    DEFINE_PROP_STRING("name", EspBusBridge, region_name),
    DEFINE_PROP_STRING("intc", EspBusBridge, intc_type),
    DEFINE_PROP_STRING("irqsock", EspBusBridge, irq_socket_path),
    DEFINE_PROP_UINT64("base", EspBusBridge, base, 0),
    DEFINE_PROP_UINT64("size", EspBusBridge, size, 0),
    DEFINE_PROP_INT32("priority", EspBusBridge, priority, 100),
    DEFINE_PROP_BOOL("shadow", EspBusBridge, shadow, false),
    DEFINE_PROP_STRING("gates", EspBusBridge, gates_spec),
    DEFINE_PROP_END_OF_LIST(),
};

static void bridge_init(Object *obj)
{
    EspBusBridge *s = ESP_BUS_BRIDGE(obj);

    s->intc_type = g_strdup("misc.esp32s3.intmatrix");
    s->fd = -1;
    s->irqfd = -1;
    s->regs = g_hash_table_new(g_direct_hash, g_direct_equal);
    s->gates = g_hash_table_new(g_direct_hash, g_direct_equal);
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
