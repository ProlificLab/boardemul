# ADR 0177 — A received WiFi frame has a signal strength

**Status:** Accepted — 2026-11-21. Amends ADR 0152's account of the Max's
WiFi join; adds to ADR 0063's receive header.

## Context

ADR 0152 left the Max's own WiFi screen unable to join: V3.1 of LilyGO's
factory firmware types a password as keypad digits, and the access point's
password was letters. Given a passphrase of digits that typing produces
(`aaaa` types `22222222`), the screen takes it and its Connect button calls
`WiFi.begin(ssid, password)` (`ui_deckpro_port.cpp`, `ui_wifi_connect`, at
the commit the image was published from). The station then failed forever,
`Reason: 201 - NO_AP_FOUND`, a probe and its answer every 80 ms.

The answers arrived. Tracing the station's own scan code, as Espressif ships
it in `libnet80211.a` for ESP-IDF 4.4.6 (the Arduino-ESP32 2.0.14 package,
the version this image was built with), placed in the image by its log
strings:

- `scan_profile_check` accepted the network every time: name, security,
  cipher and key management all matched;
- `scan_parse_beacon` added it to the connection candidates every time;
- `cnx_choose_rc` never chose it. It keeps the candidate whose utility,
  `cnx_cal_rc_util`, is greatest, compared unsigned and starting from
  zero, and for a station that sorts by signal (Arduino's default,
  `WIFI_CONNECT_AP_BY_SIGNAL`) the utility is the candidate's received
  signal strength, a signed byte. Any real signal is negative and wins;
  zero never does.

The receive header this model writes (ADR 0063) left its first byte zero,
and that byte is the frame's RSSI (`wifi_pkt_rx_ctrl_t.rssi`, signed, in
dBm). Every frame arrived at 0 dBm -- stronger than any transmitter in reach
-- and the Max's scan list printed that zero beside the network. Meshtastic
and the T-Deck's factory test joined at the same zero (ADRs 0060-0097,
0164); which branch of the choice spared them was not traced.

## Decision

**A frame the station receives arrives at −45 dBm**: the first byte of the
receive header, beside the bits ADR 0063 measured, carries it. The WiFi air
has no positions, unlike the LoRa air (ADR 0052), so this is one fixed
figure: a station across a room from its access point, well clear of any
threshold a firmware sets. `BOARDEMUL_WIFI_HEADER` still overrides the word
for experiments.

## Measured

On 2026-11-21, the Max at 0 dBm: 2,525 attempts, each a probe and an answer
the driver took, every one `NO_AP_FOUND`. With −45 dBm in the header (first
through `BOARDEMUL_WIFI_HEADER=0:0x300000d3`, then in the model): the first
attempt authenticated, associated, completed the four-way handshake and
took 192.168.4.2, with no `NO_AP_FOUND` at all.
`tests/test_max_wifi_join.py` passes, and so do the WiFi tests of both
boards (scan, connect, channels, power save, the uplink, Meshtastic's API
over WiFi).

How it was found, for the next one: QEMU's `exec` log with `-dfilter` on a
function's range (`BOARDEMUL_QEMU_DFLAGS=exec,nochain`,
`BOARDEMUL_QEMU_ARGS=-dfilter LO..HI`) counts which of its blocks run. The
functions were placed in the image by the log strings they load, read off
the library's own relocations; the library's code is what the image runs,
instruction for instruction, between calls the linker relaxed.

## Consequences

**Good**

- A station that sorts by signal joins, as on a board; the Max's factory
  screen joins a network from its own keyboard.
- Scan lists show a plausible signal instead of zero.

**Costs, accepted**

- Every frame has the same strength, so a firmware that roams, or shows
  bars, sees one unchanging value.
- On V3.1 the network's passphrase has to be one its keypad screen can type;
  that is the firmware's limit, not the model's (ADR 0152).
