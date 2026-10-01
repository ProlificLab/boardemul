# ADR 0106 — What the hardware census found harmless, and what it left open

**Status:** Accepted — 2026-10-01.

## Context

After ADR 0099, every firmware this project has was booted with the peers its
slow test gives it. Each run was then counted for what nothing answered: QEMU's
own `unimp` and `guest_errors` lines, claimed windows and the traffic they saw,
unclaimed bus accesses, I2C addresses nothing answered, and chip selects
clocked into nothing (ADR 0053's method). The firmwares were the four
T-Deck-MAX images (factory demo, WireScan, the e-paper and GPS examples), the
three T-Deck unit-test builds, and Meshtastic 2.7.10.

Most of what it found has been fixed and has its own ADR: the USB serial port
(0100), the interrupt matrix (0101), the Max's radio (0102), its fuel gauge
(0103), the guest clock (0104), and its modem and receiver (0105). This page
is for the rest. Each item was examined and found harmless, or is a known gap
not yet worked. A negative result is worth keeping for the same reason the
experiments directory keeps them: it stops the same ground being covered twice.

## Harmless, with the reason

1. **`M25P80: Unknown cmd 5a`, `Unknown cmd 77`, `Read id (command 0x90/0xAB)
   is not supported`**, on every boot. The fork models a 16 MB part as an ISSI
   `is25lp128`, which has no SFDP table (0x5A), no burst-with-wrap (0x77) and
   no legacy read-ID. The bootloader asks and boots regardless. The T-Deck's
   actual flash vendor is not documented anywhere read here, so choosing a
   part to answer these would be a guess.

2. **`Invalid read at addr 0x10200C, region 'er'`**, twice per boot. `er` is
   the Xtensa external-register space and `0x10200C` is the on-chip debug
   `DCRSET` register. The IDF reads it once to ask whether a JTAG debugger is
   attached. QEMU answers 0, "no", which is a board with no probe.

3. **`Invalid read at addr 0x0`, seven 12-byte reads, in Meshtastic's early
   boot.** The fork's GDMA fetches the "next" descriptor after the end of a
   terminated list, at link 0. On that path the result is ignored (the
   `!exit_loop` guards), so no descriptor error reaches the guest. It is log
   noise, not behaviour.

4. **`hyn_cst66xx: get tpinfo failed`** from the Max's factory demo. Its touch
   driver probes for a CST66xx before falling back to the CST328 the board
   carries. A real board prints the same, and `test_cst66xx_probe_is_rejected`
   keeps it so.

## Open, and why

5. *(Closed by ADR 0108: the hub now boots its firmware.)* **The BHI260AP
   sensor hub** answered its identity registers and nothing else, and the factory demo reports `reset bhy2 failed! ... Timed out`. What
   comes after the reset is Bosch's whole host interface: a firmware upload in
   chunks over channel 0, boot verification, a parameter protocol on the
   status channel, and virtual sensors streaming into FIFOs. A partial model
   could leave the demo waiting on a boot that never completes, where today it
   gives up and carries on. It is its own piece of work.

6. **The WiFi MAC acknowledges nothing at the 802.11 level** (ADR 0067). A
   frame the access point sends while the station is retuning is lost, where a
   real MAC would be acknowledged or retry. Higher layers retry instead (EAPOL,
   DHCP, TCP), so it costs time rather than function.

7. **The SX1262 never drives BUSY**, on either board (ADR 0102). RadioLib waits
   for it to be low, and it always is.
