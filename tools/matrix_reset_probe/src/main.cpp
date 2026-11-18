// Hold a GPIO interrupt asserted with the CPU not taking it, reset the digital
// system, and see what is left of it after the reset (ADRs 0142, 0154). The
// boot count survives the reset in RTC memory.
//
// Everything runs on core 0: the board's GPIO block routes the PRO CPU's
// interrupt enables, and Arduino's setup() runs on core 1.
#include <Arduino.h>
#include "driver/gpio.h"
#include "esp_intr_alloc.h"
#include "soc/gpio_struct.h"
#include "soc/rtc_cntl_reg.h"

RTC_NOINIT_ATTR static uint32_t magic;
RTC_NOINIT_ATTR static uint32_t boots;

// GPIO 4 is the T-Deck's battery sense input: nothing on the emulated board
// drives it, so its level is what this probe writes.
static const gpio_num_t PIN = GPIO_NUM_4;
static volatile uint32_t taken;

static void IRAM_ATTR on_gpio(void *) {
  uint32_t status = GPIO.status;
  GPIO.status_w1tc = status;
  if (status & (1u << PIN)) taken++;
}

static void say_status(const char *when) {
  Serial.printf("probe: %s, status 0x%08x\n", when, (unsigned)GPIO.status);
}

// One rising edge on the pin, and whether the handler ran for it.
static void edge(const char *what) {
  GPIO.out_w1tc = 1u << PIN;
  vTaskDelay(pdMS_TO_TICKS(50));
  uint32_t before = taken;
  GPIO.out_w1ts = 1u << PIN;
  vTaskDelay(pdMS_TO_TICKS(50));
  uint32_t after = taken;
  Serial.printf("probe: %s was taken: %s (before %u, after %u)\n", what,
                after > before ? "yes" : "no", (unsigned)before, (unsigned)after);
}

static void probe_task(void *) {
  intr_handle_t handle;

  if (boots == 1) {
    gpio_set_direction(PIN, GPIO_MODE_INPUT_OUTPUT);
    gpio_set_intr_type(PIN, GPIO_INTR_POSEDGE);
    gpio_isr_register(on_gpio, NULL, 0, &handle);
    // The CPU stops taking it; the source still reaches the matrix.
    esp_intr_disable(handle);
    gpio_set_level(PIN, 0);
    gpio_intr_enable(PIN);
    gpio_set_level(PIN, 1);
    say_status("edge raised with the CPU masked");
    Serial.println("probe: resetting the digital system");
    Serial.flush();
    SET_PERI_REG_MASK(RTC_CNTL_OPTIONS0_REG, RTC_CNTL_SW_SYS_RST);
    vTaskDelay(portMAX_DELAY);
  }

  // A digital reset resets the GPIO peripheral on silicon: the latched
  // status and the pin's configuration are gone (ADR 0154).
  say_status("after the reset");
  gpio_isr_register(on_gpio, NULL, 0, &handle);
  say_status("after gpio_isr_register");
  // Without the driver the pin is not configured, so no edge interrupts.
  gpio_set_direction(PIN, GPIO_MODE_INPUT_OUTPUT);
  edge("an edge on the pin before configuring its interrupt");
  // Configured again, as a firmware must after a digital reset: one edge,
  // one interrupt, and nothing left over from before the reset (ADR 0142).
  gpio_set_intr_type(PIN, GPIO_INTR_POSEDGE);
  gpio_intr_enable(PIN);
  edge("an edge after configuring it again");
  // Latched again with the CPU masked, so that only the driver can clear it:
  // ESP-IDF's driver clears a pin's status while it enables it.
  esp_intr_disable(handle);
  GPIO.status_w1ts = 1u << PIN;
  say_status("latched again with the CPU masked");
  gpio_intr_enable(PIN);
  say_status("after gpio_intr_enable");
  Serial.println("probe: done");
  vTaskDelete(NULL);
}

void setup() {
  Serial.begin(115200);
  if (magic != 0x4D415452u) {
    magic = 0x4D415452u;
    boots = 0;
  }
  boots++;
  Serial.printf("probe: boot %u, reset reason %d\n", (unsigned)boots,
                (int)esp_reset_reason());
  if (boots <= 2) {
    xTaskCreatePinnedToCore(probe_task, "probe", 4096, NULL, 5, NULL, 0);
  } else {
    Serial.println("probe: done");
  }
}

void loop() { delay(1000); }
