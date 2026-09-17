# ADR 0076 — Message one waits for the station to be ready

**Status:** Hypothesis REFUTED by its own test — 2026-09-17. The reading below
is kept because the page promised to keep it either way; the fix it proposed
does not work, and the addendum says why. The message-one hold ships as an
experiment knob defaulting to off, not as a fix.

## Context

With the register trace opened on message three (ADR 0075), one run gave six
association attempts and one completed handshake, and the six line up on a
single axis -- **when message one arrived, measured from the association**:

    attempt 1  msg1 at +104 ms: IGNORED, no message two for 100 ms
               msg1 again at +204 ms: answered in 10 ms, msg3 5 ms later,
               msg4 57 ms after that -- COMPLETE in 72 ms -- leaves at +992 ms
    attempts 2-6  msg1 at +103 ms: answered in 19-40 ms, msg3 at +127..172,
               then nothing; leaves at +5956..6006 ms

The peer offers message one on its first tick after the association
response, about 103 ms later, every time. The five attempts where the station
answered that first message never produced a message four. The one attempt
where the station *ignored* it, and the peer's 100 ms retry landed at +204 ms,
completed the whole handshake in 72 ms.

The reading: the station's supplicant is being set up in the hundred or two
milliseconds after association -- in the vendor stack the association event
resets the supplicant's state machine -- and message one at +103 ms lands
inside that. Answered from state that is then wiped, message two goes out
and message three finds no key to check against; dropped silently, the
station times out six seconds later. Ignored, the retry arrives after the
reset and everything works. On real silicon that setup takes microseconds and
a real access point's message one, sent at once, is fine. Here the guest runs
the whole stack under emulation, and the same setup takes long enough for the
peer to get in ahead of it.

This also explains why the handshake completes only under the wide trace
filter (ADR 0074): a slower guest makes the setup later still, so the first
message one is more often ignored and the retry more often the one that
counts. The filter was never the cause; it tilted the odds.

## Decision

**Hold message one until the station has had time to be ready.** The peer
keeps offering it on a tick, but not before `FIRST_HOLD_MS` after the
association response; `BOARDEMUL_WIFI_FIRST_MS` overrides the value for
measuring. The retry logic is unchanged.

The prediction is specific: with a hold of 300 ms, every attempt should
complete, filter or no filter. If the hold makes no difference, the reading
above is wrong and this page says so.

## Consequences

**Good**

- A one-constant change in the Python peer, no device change, no rebuild.

- If it holds, the handshake becomes reproducible in an ordinary run, which
  is what a test needs (ADR 0074's first cost).

**Costs, accepted**

- **A fixed hold is a guess at the station's speed.** A slower guest -- more
  tracing, a slower host -- could still get ahead of it. The retry stays as
  the backstop, and the right long-term answer is for the peer to retry
  sooner rather than to wait longer.

- **It is a departure from what a real access point does**, which sends at
  once. Recorded so no one later "fixes" it back.

## Alternatives rejected

**Retry faster instead of holding.** The failing case is not a lost message
but an answered one: the station replies, and the reply is worthless. A
faster retry would only race the reset more often.

**Leave it, and rely on the retry.** One completion in six is what that gives.


## Addendum — the hold changes nothing, so the race is not where this looked

The decision above made a falsifiable prediction: a 300 ms hold should make
every attempt complete. It was run at 300 ms and at 200 ms, against the
unheld baseline:

| hold | attempts | completions |
|---|---|---|
| none | 6 | 1 |
| 200 ms | 5 | 1 |
| 300 ms | 6 | 2 |

The completion rate is flat within noise, and at 200-300 ms the hold sometimes
stopped message one being answered at all -- the station gave up waiting and
re-associated. The prediction failed. Per this page's own terms, the reading
that message one races the supplicant's *setup after association* is therefore
wrong, or at least is not the lever.

What is still true, and now better explained: the handshake completes only
when the guest is slowed by the wide trace filter (ADR 0074). A peer-side hold
delays when the *peer* sends; it cannot change how fast the *guest* runs
between receiving message three and checking it against the key it derived for
message two. If the race is inside the guest -- the supplicant checking
message three before its own message-two post-processing has settled, which a
lock or a slower core would perturb -- then only guest speed moves it, and the
filter does exactly that while the hold does nothing. That matches every
measurement here.

So the next place to look is inside the guest between message two going out and
message three arriving, not in the peer's timing. The `BOARDEMUL_WIFI_FIRST_MS`
knob stays, defaulting to 0, for anyone who wants to re-test the peer-timing
idea; it is not a fix and the default does not change the peer's behaviour.

## What the run did establish

Two findings from the same trace stand on their own, independent of the
refuted hold:

- **The MICs verify byte-for-byte on the wire.** Message two from the station
  and message three from the peer were captured in full and both MICs
  recompute exactly from the PTK derived from the passphrase and the two
  nonces. The key schedule is not in question.

- **The key-install registers are located.** When a handshake completes, the
  driver writes a cluster the device has never modelled -- offsets 0x12f8,
  0x12fc, 0x1310, 0x1314, 0x1318 around message four -- and then a second
  burst at 0x000, 0x004, 0x020, 0x024, 0x064, 0x0d8, 0x800, 0x810. A failing
  attempt touches none of them. These are the crypto and key-slot control
  interface ADR 0064 set out to model, and the device currently keeps them as
  plain storage. They are the next subject.
