# ADR 0167 — Slow tests whose firmware keeps deadlines on the host's time run alone

**Status:** Accepted — 2026-11-20.

## Context

The slow tier is run with `pytest -m slow`, one test at a time (ADR 0012).
Run three files at a time to finish sooner, it failed on 2026-11-19 and
again on 2026-11-20, and always in the same places: the factory firmware's
WiFi connection and uplink (`test_wifi_connect`, `test_wifi_uplink`), the
Max's WiFi screen (`test_max_wifi`), and Meshtastic's interface
(`test_meshtastic_touch`, `test_backlight`, `test_meshtastic_api`). Each of
them passes alone, on the same build, the same day.

What they share is a firmware deadline measured on a guest clock that
follows the host's. Without `-icount` an emulated second is a wall second
whatever the cores did in it (ADR 0115), so a busier host gives the
firmware fewer instructions per second of its own time:

- WPA2's four-way handshake gives a station three seconds to answer, as
  hostapd does (ADR 0095); a station that answers late is deauthenticated.
- Meshtastic's colour interface re-requests its configuration after a few
  seconds, and under load re-requests it for minutes, filling the node's
  queue to the phone until an assertion fires (ADR 0164).

`-icount` would make those deadlines the firmware's own, and costs too
much wall time to be the default: measured on 2026-11-19, a Meshtastic join
took 825 s at `shift=4` against four to nine minutes without, and a larger
shift did not make it faster. The four-way handshake also needs two
translation threads (ADR 0066), which `-icount` does not allow.

## Decision

1. **A marker, `alone`**, registered in `pytest.ini`, on the slow test
   files whose firmware keeps such a deadline: the six above. It says
   something true about the test, not about a runner: it measures behaviour
   that depends on the host having time to spare.

2. **`tools/slow_suite.sh [N]`** runs the slow tier N files at a time,
   then the `alone` files one at a time with nothing else booting. It
   replaces a working script under `logs/` that ran everything N at a time.
   `pytest -m slow` stays the plain way, and is already one at a time.

3. **A test is marked `alone` on evidence**: it failed when sharing the
   host and passed alone on the same build. Marking is not a way to quiet a
   test that fails alone; that is a defect, found the usual way.

## Consequences

**Good**

- A parallel run of the slow tier passes when its parts do, and finishes
  sooner than one at a time.
- The list of host-sensitive tests is in the code, where it can be read and
  shortened as deadlines move onto the guest's own clock.

**Costs, accepted**

- The `alone` files run serially at the end: about a quarter of the tier's
  time.
- The marker records a limit rather than removing it: these firmwares miss
  their deadlines on a loaded host in normal runs too, which the guide
  already says.
