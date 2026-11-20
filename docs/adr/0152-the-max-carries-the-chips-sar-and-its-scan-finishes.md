# ADR 0152 — The Max carries the chip's SAR block, and its WiFi scan finishes

**Status:** Accepted — 2026-11-18. Corrects ADR 0137's evidence, and applies
its reasoning once more.

## Context

ADR 0137 wired the T-Deck's WiFi MAC into the Max board and said the
launcher's WiFi screen answers: Scan runs, and with an access point on the
air the network is listed. Its slow test taps the WiFi icon, then a point
it calls the Scan button, and passes on a new frame.

Driven step by step on 2026-11-18, with each frame kept, the launcher's
WiFi icon opens a menu of two entries, "WIFI Config" and "WIFI Scan". The
test's "Scan button", (39, 91), falls inside the first entry. It opened the
config screen, which is blank, and the new frame was that. The scan was
never pressed.

Pressed for real (WIFI Scan, then its Scan button), the scan never finishes.
Two minutes after the press the screen still says "Press Scan to search
WiFi", and the access point's frame channel closes having carried nothing.
Sampling the CPU found it spinning in one loop:

    4207187b: or    a2, a2, a3        ; a3 = BIT(24)
    42071881: s32i  a2, a8, 80        ; a8 = 0x60008800
    42071887: l32i  a2, a8, 80
    4207188a: bbci  a2, 8, 42071884   ; until bit 8

`0x60008800 + 0x50` is `SENS_SAR_TSENS_CTRL_REG` in ESP-IDF's `sens_reg.h`.
Bit 24 is `SENS_TSENS_DUMP_OUT`, bit 8 `SENS_TSENS_READY`: the temperature
sensor read the WiFi PHY makes while it starts. Nothing answers it on the
Max. The SENS block is modelled on the T-Deck (`SarAdc`, ADR 0020), which
answers both the SAR ADC's one-shot conversion and this read. On the Max it
is not claimed at all.

The SENS block is the ESP32-S3's, as the WiFi MAC, the I2S controllers and
the Bluetooth core are (ADRs 0137, 0148).

## Decision

1. **The Max board carries the T-Deck's `SarAdc`** at 0x60008800, 0x400
   bytes, unchanged: the temperature sensor's read answers at once with its
   fixed reading, and a one-shot conversion with its fixed sample.

2. **The Max's WiFi test drives the real flow and asserts what it shows**:
   the WiFi icon, "WIFI Scan", its Scan button, then the network's row drawn
   under the button once the panel has settled.

## Measured

On 2026-11-18, with the SENS block in place, the scan lists `BoardTest`
under the Scan button, and the access point's channel carried 438 beacons
and a probe exchange.

Joining from the factory interface goes further and stops at the firmware.
Tapping the network opens its password screen ("SSID: BoardTest", Connect,
Back). The firmware refuses an empty password ("Password cannot be empty"),
so an open network cannot be joined from it. Typing a password on the
keyboard reaches the keypad driver as the right keys: its own log reads
`k=24, … b`, `k=1, … o`, `k=19, … a`, one event at the press and one at the
release, as the TCA8418 reports them. The password field then fills with
phone-keypad digits, two per key ("boardtest" became `2266227733883337788`),
which is the mapping of the firmware's own dialer (`phone_keypad_to_digit`).
Why the dialer's mapping reaches the password field is the firmware's, and
not chased here.

*Amended 2026-11-20:* chased, as far as the sources go. Our keypad model's
events are the ones LilyGO's driver decodes (`peri_keypad.cpp`: a press is
129 to 163, a release 1 to 35, bit 7 the difference). LilyGO's current
factory source handles the WiFi password screen on its own (`entry4_3`, a
20 ms timer draining keypad events, characters inserted on presses only,
through its own letter maps, not the phone keypad's). That screen came in
with "Add WIFI connection", committed on 2026-05-22; the firmware here is
V3.1 of 2026-03-13, and LilyGO published V3.3 on 2026-10-09. The doubled
dialer digits are V3.1's own early screen. Joining from the Max's interface
needs V3.3 in the firmware library, which is a download to be agreed. The WiFi stack the Max shares with the T-Deck joins
networks in the T-Deck's tests (ADRs 0074–0097).

## Consequences

**Good**

- The Max's WiFi scan finishes and lists the access point, which ADR 0137
  claimed and had not shown.
- A test that passed on any new frame now passes only on the frame it
  means.

**Costs, accepted**

- The Max now reads the T-Deck's fixed battery sample if it ever converts on
  the SAR ADC. Its factory firmware reads its battery from the fuel gauge
  (ADR 0103), not the ADC.
- Joining a network from the Max's own interface is not shown, for the
  firmware's reason above.
- `test_max_keyboard` failed once in three runs that day, on the WiFi menu
  not redrawing for UP inside its wait, and passed alone and after this
  test. An e-paper redraw's timing, not this change; noted, not fixed.
