# ADR 0187 — A key is held for its time on the guest's clock

**Status:** Accepted — 2026-11-24.

## Context

`tests/test_max_wifi_join.py` failed on every run: the Max authenticated
and the access point rejected message two of the WPA2 handshake ("mic did
not verify"), which is what a station with the wrong passphrase sends. On
the host's clock (`--host-clock`) the same test passed, all five checks.

The test types the passphrase on the Max's keyboard, a key at a time, each
held 0.15 s. V3.1's password field types digits, two for each key held that
long (ADR 0152): it adds one per scan of a held key. The TCA8418's model
took the press and the release when the host sent them, so the hold the
firmware saw was 0.15 s of the host's time, which on the guest's
instruction-counted clock (the default since ADR 0178) is a different
length -- and a different number of digits, and another passphrase.

## Decision

On the Max, a key's release reaches the TCA8418 when the guest's clock has
run as long as the host held the key: the press goes in at once, with the
guest time it went in, and the release waits in the input ticker until the
guest is that far on. Without a guest clock yet, a release goes in at once,
as before.

## Consequences

**Good**

- A hold means the same to the firmware on either clock, so what a test or
  a person types is what the firmware gets.

**Costs, accepted**

- A release can reach the firmware later than the host let go, when the
  guest runs slower than the host; that is the hold the host asked for.
- The T-Deck's keyboard is its own microcontroller's model, which hands the
  firmware characters, not holds (ADR 0127), and is not changed.
