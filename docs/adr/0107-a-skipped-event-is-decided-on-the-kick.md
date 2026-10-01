# ADR 0107 — A skipped Bluetooth event is decided on the kick

**Status:** Accepted — 2026-10-01. Refines how ADR 0025's rule is applied.

## Context

`test_bluetooth_connect` passed on an idle host and failed under load. That had
been put down to the central, which speaks on wall time. A loaded run with the
QEMU log kept showed something else:

- the connection was made, and its first event ended with a packet;
- its second event, kicked 0.86 ms *before* its start, was reported skipped;
- after that the link layer kicked nothing more, and in four minutes the guest
  got 30 ms further.

ADR 0025's rule is that an entry *already past* when it is kicked is skipped.
The core applied it later, when the event's end timer ran: skipped if that was
more than two event lengths after the start. With a punctual timer the two are
the same test. But `--icount sleep=on` warps the clock across idle periods by
the host time that passed, and on a loaded host the main loop that runs device
timers is late. So the end timer can run well after its deadline although the
guest did everything on time. The core then reported a skip the controller
would never see, and on a connection event that wedged the link layer.

## Decision

1. **Whether an event is skipped is decided when it is kicked**, from the
   guest's own clock at that moment: skipped if the kick comes more than two
   event lengths after its start. That is exactly the threshold the end-time
   test amounted to with a punctual timer, so no decision changes on a host
   that keeps up.

2. **The end timer only reports.** When it runs no longer affects what it
   reports.

## Consequences

**Good**

- Under seven concurrent boots the link test passes: 3 of 3, plus the run
  that found this, 56 connection events with a packet each and none skipped.
  The same load failed it before.

**Costs, accepted**

- An event the guest kicked in time is reported ended even if QEMU ran its
  timer late. That is what the silicon would do. The guest, not the
  emulator's scheduling, decides whether it met its deadline.
