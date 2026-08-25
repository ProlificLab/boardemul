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
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "exec/address-spaces.h"
#include "exec/memory.h"
#include "qapi/error.h"
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

#define CMD_UPDATE  (1u << 23)
#define CMD_USR     (1u << 24)

#define USER_USR_MOSI (1u << 27)
#define USER_USR_MISO (1u << 28)

#define SPI_REG_COUNT   (0x1000 / 4)
#define SPI_FIFO_BYTES  64
#define SPI_BUFFER_MAX  (32 * 1024)

/* One segment of a chip-select window. `dc` is the level of the data/command
 * pin while these bytes were clocked, which is the only thing that
 * distinguishes a command from payload on a three-wire display. */
typedef struct QEMU_PACKED {
    uint8_t type;      /* 0 = write only, no reply; 1 = duplex, reply expected */
    uint8_t dc;
    uint16_t pad;
    uint32_t len;
} EspSpiSegment;

#define SEGMENT_WRITE  0
#define SEGMENT_DUPLEX 1

struct EspSpiMaster {
    DeviceState parent_obj;

    MemoryRegion iomem;
    char *socket_path;
    char *region_name;
    uint64_t base;
    uint64_t size;
    int32_t priority;
    int32_t cs_gpio;
    int32_t dc_gpio;
    bool miso;

    uint32_t regs[SPI_REG_COUNT];
    uint8_t fifo[SPI_FIFO_BYTES];

    /* Bytes accumulated since the last flush, all sharing one dc level. */
    uint8_t *buffer;
    uint32_t buffered;
    int buffered_dc;

    int fd;
    bool broken;
};

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

/* Hand the accumulated bytes to the model. Nothing is expected back: a
 * write-only burst is the overwhelming majority of display traffic, and not
 * waiting is what makes batching worth doing. */
static void spi_flush(EspSpiMaster *s)
{
    EspSpiSegment seg;

    if (!s->buffered || s->broken || s->fd < 0) {
        s->buffered = 0;
        return;
    }
    seg.type = SEGMENT_WRITE;
    seg.dc = (uint8_t)(s->buffered_dc & 1);
    seg.pad = 0;
    seg.len = s->buffered;
    if (!spi_write_all(s, &seg, sizeof(seg)) ||
        !spi_write_all(s, s->buffer, s->buffered)) {
        spi_fail(s, "send failed");
    }
    s->buffered = 0;
}

static void spi_append(EspSpiMaster *s, const uint8_t *data, uint32_t len)
{
    int dc = s->dc_gpio >= 0 ? esp_gpio_pin_level(NULL, s->dc_gpio) : 0;

    /* SPI2 is a shared bus. A device only sees bytes while its own chip-select
     * is asserted, so anything clocked out for a different device on the same
     * pins is not ours to forward. */
    if (s->cs_gpio >= 0 && esp_gpio_pin_level(NULL, s->cs_gpio)) {
        return;
    }

    /* A change of data/command mid-window starts a new segment; so does
     * filling the buffer, which only bounds memory and is otherwise invisible
     * to the model. */
    if (s->buffered && dc != s->buffered_dc) {
        spi_flush(s);
    }
    if (s->buffered + len > SPI_BUFFER_MAX) {
        spi_flush(s);
    }
    s->buffered_dc = dc;
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

    spi_flush(s);
    if (s->broken || s->fd < 0) {
        memset(data, 0, len);
        return;
    }
    seg.type = SEGMENT_DUPLEX;
    seg.dc = (uint8_t)(s->dc_gpio >= 0 ? esp_gpio_pin_level(NULL, s->dc_gpio) : 0);
    seg.pad = 0;
    seg.len = len;
    if (!spi_write_all(s, &seg, sizeof(seg)) ||
        !spi_write_all(s, data, len) ||
        !spi_read_all(s, data, len)) {
        spi_fail(s, "duplex exchange failed");
        memset(data, 0, len);
    }
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
    if ((user & USER_USR_MISO) && s->miso) {
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

    /* Chip-select is active low, so a rising edge ends the transaction. */
    if (level) {
        spi_flush(s);
    }
}

static uint64_t spi_read(void *opaque, hwaddr offset, unsigned size)
{
    EspSpiMaster *s = opaque;

    if (offset >= R_W0 && offset <= R_W15 + 3) {
        uint32_t i = offset - R_W0;
        return ldl_le_p(s->fifo + (i & ~3u));
    }
    if (offset / 4 >= SPI_REG_COUNT) {
        return 0;
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
         * makes the driver's own read-modify-write re-fire the transfer. */
        s->regs[R_CMD / 4] = value & ~(CMD_USR | CMD_UPDATE);
        if (value & CMD_USR) {
            spi_start(s);
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
    if (s->cs_gpio >= 0) {
        esp_gpio_watch(NULL, s->cs_gpio, spi_cs_changed, s);
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
    DEFINE_PROP_INT32("dc_gpio", EspSpiMaster, dc_gpio, -1),
    DEFINE_PROP_BOOL("miso", EspSpiMaster, miso, true),
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
