// Answer each line typed into either serial port (ADR 0156): UART0, which
// Arduino calls Serial here, and the USB Serial/JTAG port, USBSerial.
#include <Arduino.h>

static void answer(Stream &port, const char *name) {
  if (!port.available()) return;
  String line = port.readStringUntil('\n');
  line.trim();
  port.printf("probe: %s heard: %s\n", name, line.c_str());
}

void setup() {
  Serial.begin(115200);
  USBSerial.begin();
  delay(200);
  Serial.println("probe: uart listening");
  USBSerial.println("probe: usb listening");
}

void loop() {
  answer(Serial, "uart");
  answer(USBSerial, "usb");
  delay(10);
}
