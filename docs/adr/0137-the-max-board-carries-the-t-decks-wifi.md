# ADR 0137 — The Max board carries the T-Deck's WiFi

**Status:** Accepted — 2026-10-21.

## Context

The Max board was wired without a WiFi MAC: `--wifi-ap` was refused with
"this board has no WiFi MAC modelled", and the launcher's WiFi screen
proved the point — pressing Scan asked hardware nobody answered, and the
guest waited forever.

But the WiFi MAC is a property of the ESP32-S3, which both boards share.
The whole station stack — the MAC device in C, the PHY pages and their
completion gates, the air, the access point, WPA2, the four-way handshake,
CCMP — was built for the T-Deck (ADRs 0056–0098) and none of it is
board-specific.

## Decision

**The Max board's build wires the T-Deck's WiFi**, unchanged:

- the `esp-wifi-mac` device at 0x60033000, on ETS_WIFI_MAC_INTR_SOURCE;
- the PHY pages as claimed trace windows — `radio_unknown`, `fe`, `nrx`,
  `bb`, `wifi_mac3`, `wifi_mac4`;
- `PhyGates`, moved to `emu/board.py` because the gates are the chip's:
  the ROM and app polling loops it answers run here too once the launcher's
  WiFi stack starts;
- `--wifi-ap NAME[,MS[,CHANNEL]]` puts the access point on the air, exactly
  as the T-Deck's does, and without one the scan runs and correctly finds
  nothing.

The web API's start call takes optional run arguments, so a browser session
can ask for an access point the same way a flag would.

## Consequences

**Good**

- The launcher's WiFi screen answers: Scan runs, and with an access point
  on the air the network is listed — driven through the web API in the
  slow test, the same trip the browser makes.

**Costs, accepted**

- The connection flow (WPA2, DHCP, the launcher's own connection UI) is
  exercised by the T-Deck's tests but not yet by a Max-firmware test; the
  scan is the proven edge for now.
