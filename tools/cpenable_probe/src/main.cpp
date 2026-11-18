// Read CPENABLE where the T-Deck's vendor firmware runs its first float: in a
// C++ global constructor on the PRO CPU, before FreeRTOS starts and before
// anything has written the register (ADR 0143). Upstream measured silicon the
// same way, with an rsr.cpenable before any software writes it, and read 0xff
// (espressif/qemu#154, commit 82ff7118).
//
// Once on each kind of boot: power-on, then esp_restart() (RTC_SW_CPU_RST),
// then a digital system reset through SW_SYS_RESET (RTC_SW_SYS_RST). The boot
// count survives both resets in RTC memory.
#include <Arduino.h>
#include "soc/rtc_cntl_reg.h"

RTC_NOINIT_ATTR static uint32_t magic;
RTC_NOINIT_ATTR static uint32_t boots;

static uint32_t cpenable_at_ctors;
static uint32_t core_at_ctors;

__attribute__((constructor)) static void read_cpenable(void) {
  uint32_t value;
  __asm__ __volatile__("rsr.cpenable %0" : "=a"(value));
  cpenable_at_ctors = value;
  core_at_ctors = xPortGetCoreID();
}

void setup() {
  Serial.begin(115200);
  if (magic != 0x43504E45u) {
    magic = 0x43504E45u;
    boots = 0;
  }
  boots++;
  Serial.printf("probe: boot %u, reset reason %d, CPENABLE at the constructors "
                "0x%08x on core %u\n",
                (unsigned)boots, (int)esp_reset_reason(),
                (unsigned)cpenable_at_ctors, (unsigned)core_at_ctors);
  Serial.flush();
  if (boots == 1) {
    Serial.println("probe: restarting");
    Serial.flush();
    esp_restart();
  } else if (boots == 2) {
    Serial.println("probe: resetting the digital system");
    Serial.flush();
    SET_PERI_REG_MASK(RTC_CNTL_OPTIONS0_REG, RTC_CNTL_SW_SYS_RST);
    for (;;) {
    }
  }
  Serial.println("probe: done");
}

void loop() { delay(1000); }
