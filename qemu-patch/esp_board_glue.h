/*
 * Shared declarations between the BoardEmul SoC models.
 *
 * These files live in the BoardEmul repository and are installed into the QEMU
 * tree at build time (ADR 0005, ADR 0011). Nothing here is board specific: a
 * pin is a number, and which number carries chip-select is a property supplied
 * on the command line.
 */

#ifndef ESP_BOARD_GLUE_H
#define ESP_BOARD_GLUE_H

#include "qemu/osdep.h"
#include "hw/qdev-core.h"

#define TYPE_ESP_GPIO_BLOCK "esp-gpio-block"
#define TYPE_ESP_SPI_MASTER "esp-spi-master"
#define TYPE_ESP_I2C_MASTER "esp-i2c-master"

/*
 * Current level of one pin, output or externally driven. Returns 0 for a pin
 * nothing has touched, which matches a line with no driver.
 *
 * The SPI model uses this to tell a command byte from a data byte, and to know
 * when a transaction has ended -- both of which are properties of the wiring,
 * not of any device on the far end.
 */
int esp_gpio_pin_level(DeviceState *dev, unsigned pin);

/*
 * Registers a callback invoked whenever a watched pin changes level, so a
 * consumer does not have to poll. `opaque` is passed straight back.
 */
typedef void (*EspGpioWatchFn)(void *opaque, unsigned pin, int level);
void esp_gpio_watch(DeviceState *dev, unsigned pin,
                    EspGpioWatchFn fn, void *opaque);

/*
 * Drive an input pin from inside the machine, as the host socket does from
 * outside it but on the guest's clock. For a line whose timing the guest
 * measures in microseconds -- a part's BUSY (ADR 0111) -- a message from the
 * host would race the guest's read.
 */
void esp_gpio_drive(DeviceState *dev, unsigned pin, int level);

/*
 * Drive an input pin to `level` at guest time `at_ns` (QEMU's virtual clock).
 * One change is pending per pin; a newer one replaces it, and a negative
 * `at_ns` only cancels. A time already past drives the pin at once. For a
 * part that knows ahead of time when its line will move -- a radio's TxDone
 * at the end of a packet (ADR 0112).
 */
void esp_gpio_schedule(DeviceState *dev, unsigned pin, int level, int64_t at_ns);

#endif /* ESP_BOARD_GLUE_H */
