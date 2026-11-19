// Light sleep wakes on its timer and on a pin (ADR 0165). Twice, each time
// saying what woke it, by esp_sleep_get_wakeup_cause()'s numbering
// (ESP_SLEEP_WAKEUP_TIMER is 4, ESP_SLEEP_WAKEUP_GPIO is 7):
//   1. two seconds on the RTC timer;
//   2. until GPIO 0, the T-Deck's trackball press, is low.
#include <Arduino.h>
#include "driver/gpio.h"
#include "esp_sleep.h"

void setup() {
  Serial.begin(115200);
  delay(200);

  Serial.println("probe: sleeping 2000 ms on the timer");
  Serial.flush();
  uint32_t before = millis();
  esp_sleep_enable_timer_wakeup(2000 * 1000ULL);
  esp_err_t err = esp_light_sleep_start();
  Serial.printf("probe: woke, err %d, cause %d, after %lu ms\n", (int)err,
                (int)esp_sleep_get_wakeup_cause(),
                (unsigned long)(millis() - before));

  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_TIMER);
  gpio_wakeup_enable(GPIO_NUM_0, GPIO_INTR_LOW_LEVEL);
  esp_sleep_enable_gpio_wakeup();
  Serial.println("probe: sleeping until GPIO 0 is low");
  Serial.flush();
  err = esp_light_sleep_start();
  Serial.printf("probe: woke, err %d, cause %d\n", (int)err,
                (int)esp_sleep_get_wakeup_cause());
  Serial.println("probe: done");
}

void loop() { delay(1000); }
