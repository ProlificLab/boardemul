// The USB OTG probe (ADR 0166). Serial is TinyUSB's CDC port on the S3's OTG
// controller; Serial0 is the UART, where the probe says how the port came up.
//   1. it waits for a host to open the port (DTR), and says when;
//   2. it writes twenty lines on the port, more than a packet's worth each
//      time the FIFO empties, and then "probe: ready";
//   3. it answers each line typed to it with "echo: <line>".
#include <Arduino.h>

static String line;

void setup() {
  Serial0.begin(115200);
  Serial0.println("probe: starting USB");
  Serial.begin(115200);

  uint32_t start = millis();
  while (!Serial && millis() - start < 30000) {
    delay(10);
  }
  Serial0.printf("probe: port %s after %lu ms\n", Serial ? "open" : "closed",
                 (unsigned long)(millis() - start));

  for (int i = 0; i < 20; i++) {
    Serial.printf("probe: line %02d, long enough that a few make a packet\n", i);
  }
  Serial.println("probe: ready");
  Serial.flush();
  Serial0.println("probe: wrote its lines");
}

void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      Serial.printf("echo: %s\n", line.c_str());
      Serial0.printf("probe: echoed %u byte(s)\n", (unsigned)line.length());
      line = "";
    } else if (c != '\r') {
      line += c;
    }
  }
  delay(5);
}
