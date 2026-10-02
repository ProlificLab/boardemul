// Restart the chip once from each core, both ways ESP-IDF does it, and say
// before each one which it is. Boot count survives a software reset in RTC
// memory, so the plan advances one step per boot and then stops.
#include <Arduino.h>
#include "esp_system.h"

RTC_NOINIT_ATTR static uint32_t magic;
RTC_NOINIT_ATTR static uint32_t boots;

struct Step { int core; bool panic; };
static const Step plan[] = {{1, false}, {0, false}, {1, true}, {0, true}};
static const int steps = sizeof(plan) / sizeof(plan[0]);

static void restart_task(void *arg) {
  const Step *s = (const Step *)arg;
  Serial.printf("probe: %s on core %d\n", s->panic ? "abort" : "esp_restart",
                xPortGetCoreID());
  Serial.flush();
  if (s->panic) abort();
  esp_restart();
}

void setup() {
  Serial.begin(115200);
  if (magic != 0x5EB007EDu) {
    magic = 0x5EB007EDu;
    boots = 0;
  }
  boots++;
  Serial.printf("probe: boot %u, reset reason %d\n", (unsigned)boots,
                (int)esp_reset_reason());
  delay(100);
  if (boots <= (uint32_t)steps) {
    const Step *s = &plan[boots - 1];
    xTaskCreatePinnedToCore(restart_task, "probe", 4096, (void *)s, 5, NULL,
                            s->core);
  } else {
    Serial.println("probe: done");
  }
}

void loop() { delay(1000); }
