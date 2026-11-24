// SPDX-FileCopyrightText: 2026 Prolific Lab
// SPDX-License-Identifier: Apache-2.0
//
// Joins WIFI_NAME and serves /status on port 80. It says on its serial port
// what it does, so a test can wait for each step.

#include <Arduino.h>
#include <WebServer.h>
#include <WiFi.h>

static WebServer server(80);
static unsigned long requests = 0;

static void status() {
    requests++;
    server.send(200, "application/json",
                String("{\"ok\": true, \"requests\": ") + requests +
                    ", \"ip\": \"" + WiFi.localIP().toString() + "\"}");
}

void setup() {
    Serial.begin(115200);
    Serial.println("wifi-http: joining " WIFI_NAME);
    WiFi.mode(WIFI_STA);
    // BoardEmul does not yet wake a station in modem sleep for the frames
    // its access point holds (the default on Arduino), so replies are lost;
    // until it does, keep the radio awake. See CHANGELOG, "Known limits".
    WiFi.setSleep(false);
    WiFi.begin(WIFI_NAME, WIFI_PASSWORD);
    while (WiFi.status() != WL_CONNECTED) {
        delay(250);
    }
    Serial.print("wifi-http: address ");
    Serial.println(WiFi.localIP());
    server.on("/status", status);
    server.begin();
    Serial.println("wifi-http: serving on port 80");
}

void loop() {
    server.handleClient();
    delay(2);
}
