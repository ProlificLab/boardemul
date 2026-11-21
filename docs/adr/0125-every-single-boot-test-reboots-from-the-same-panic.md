# ADR 0125 — Every single-board boot test reboots from the same panic

**Status:** Accepted — 2026-10-03. Extends ADRs 0114 and 0124 to the slow
tests that still ran with `-no-reboot`.

## Context

A measurement of the wall clock against the instruction-counted one
(`logs/clock_measure.sh`) found two of its failures were not what they seemed.
`test_backlight` and `test_gps_boot` both ended in a guest reset,
`guest exited (code 0)`, at 21 s and 17 s. Neither runs with `--reboot`, so
the reset ended QEMU.

`test_backlight` had failed this way before, in full-suite runs: the screen
lit, `changes: 3`, and no fade. That was put down to the fade's animation
jumping when the host stalled. It was not looked at closely enough. A run that
ends at 21 s ends before the interface's 30-second timeout, and leaves exactly
those three changes. Seven reruns on the wall clock, four of them in parallel,
did not reset, so the cause was never captured. The likeliest one is ADR
0099's ipc1 overflow, the firmware's own:

- `test_gps_boot` boots vendor build 250620, the build ADR 0099 traced it on;
- Meshtastic hits it too (ADR 0099), and `test_mesh` already reboots from it;
- the Max's factory demo hit it once in about forty boots (ADR 0124).

Either way, a test that ends at a reset tells nobody why. ADRs 0114 and 0124
made the T-Deck boot test and the Max's test reboot, forgive that one panic by
name, and fail any other reset with the boot that ended in it.

## Decision

1. **These tests run with `--reboot` and judge the boot that came back:**
   - `test_backlight`;
   - `test_gps_boot`;
   - `test_boot_meshtastic`;
   - `test_usb_serial_boot`;
   - `test_wifi_scan`;
   - `test_wifi_connect`;
   - `test_wifi_uplink`.

   What each waits for, and what it asserts on the guest's output, is read
   from the last boot. `Run.last_uart` and `Run.last_usb` give it.
2. **Each gains `test_it_reboots_only_from_the_forgiven_panic`.** It uses
   `reset_trouble`, which puts ADR 0114's two checks into one message. Any
   panic but ADR 0099's ipc1 canary fails it, and so does a reboot for any
   other reason. The message carries the boot that ended.
3. **A pass that needed a second boot warns**, with the count, as the boot test
   does.
4. **`test_meshtastic_touch` keeps `-no-reboot`.** Its script drives a single
   boot tap by tap, and the firmware's own restart after OK is the end it
   waits for. A reboot partway through could not be resumed, and `--reboot`
   would change what it tests.
5. **What a part outside the chip records spans both boots**, as for the
   panel's frames in ADR 0114: the backlight's duty changes, the GPS module's
   log, the access point's side of the conversation. Those parts are on the
   board, and a board's parts do not reset with its chip.

## Consequences

**Good**

- A reset in any of these tests is either the firmware's known overflow,
  survived as a board survives it, or a failure that names its panic and
  shows the boot it ended.
- The next "3 changes" in `test_backlight` will say why, or will not happen.

**Costs, accepted**

- A run that hits the canary takes another boot's worth of wall time.
- If these resets are not the canary, this ADR does not fix them. It makes
  them fail with their cause in the message.
- `test_meshtastic_touch` can still be ended by the canary, about one run in
  ten to twenty, as before.

*Amended 2026-11-21:* `test_meshtastic_touch` came to run with `--reboot`
after all (its retries made a restarted boot resumable). `test_device_boot`
was the one left: a full slow-tier run ended its first boot at 13 s in the
ipc1 canary, `guest exited (code 0)`, and the test then waited five minutes
for a setup dialog. It runs with `--reboot` too. Its first boot ends in the
restart the firmware asks for after OK, so that boot is checked for any
panic but the canary; the second, which asks for none, by `reset_trouble`.
