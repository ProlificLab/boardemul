// Record from I2S1 and say what was heard, then play a tone on I2S0 (ADR
// 0146). The T-Deck's own wiring: the microphone codec on I2S1, the speaker
// amplifier on I2S0. Both 16-bit stereo at 16 kHz, as the vendor UnitTest
// build sets them (ADR 0119), through ESP-IDF 4.4's I2S driver.
//
// What was heard is said as an RMS level and the strongest frequency between
// 100 Hz and 2 kHz, found by a Goertzel scan of the left channel.
#include <Arduino.h>
#include <math.h>
#include "driver/i2s.h"

static const int RATE = 16000;
static const int SECONDS = 1;
static int16_t frames[RATE * 2];        // one second, stereo

static void install(i2s_port_t port, i2s_mode_t mode) {
  i2s_config_t config = {};
  config.mode = (i2s_mode_t)(I2S_MODE_MASTER | mode);
  config.sample_rate = RATE;
  config.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  config.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  config.dma_buf_count = 8;
  config.dma_buf_len = 256;
  i2s_driver_install(port, &config, 0, NULL);
}

static double goertzel(const int16_t *left, int n, double hz) {
  double k = 2 * cos(2 * M_PI * hz / RATE), s1 = 0, s2 = 0;
  for (int i = 0; i < n; i++) {
    double s0 = left[2 * i] + k * s1 - s2;
    s2 = s1;
    s1 = s0;
  }
  return s1 * s1 + s2 * s2 - k * s1 * s2;
}

void setup() {
  Serial.begin(115200);
  delay(200);

  Serial.println("probe: recording");
  install(I2S_NUM_1, I2S_MODE_RX);
  size_t got = 0;
  // The first buffers can predate the stream settling; read a second of it.
  i2s_read(I2S_NUM_1, frames, sizeof(frames), &got, portMAX_DELAY);
  int n = got / 4;
  double sum = 0;
  for (int i = 0; i < n; i++) sum += (double)frames[2 * i] * frames[2 * i];
  double rms = n ? sqrt(sum / n) : 0;
  int best = 0;
  double best_power = 0;
  for (int hz = 100; hz <= 2000; hz += 10) {
    double power = goertzel(frames, n, hz);
    if (power > best_power) {
      best_power = power;
      best = hz;
    }
  }
  Serial.printf("probe: heard %d frames, rms %.0f, strongest %d Hz\n", n, rms, best);
  i2s_driver_uninstall(I2S_NUM_1);

  install(I2S_NUM_0, I2S_MODE_TX);
  for (int i = 0; i < RATE; i++) {
    int16_t v = (int16_t)(12000 * sin(2 * M_PI * 1000.0 * i / RATE));
    frames[2 * i] = frames[2 * i + 1] = v;
  }
  size_t put = 0;
  for (int s = 0; s < SECONDS; s++) {
    i2s_write(I2S_NUM_0, frames, sizeof(frames), &put, portMAX_DELAY);
  }
  // Let the last buffers drain before the stream stops.
  delay(300);
  i2s_driver_uninstall(I2S_NUM_0);
  Serial.println("probe: played 1000 Hz for 1 s");
  Serial.println("probe: done");
}

void loop() { delay(1000); }
