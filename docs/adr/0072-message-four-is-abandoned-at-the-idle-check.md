# ADR 0072 — Message four is abandoned at the idle check, not the cipher

**Status:** Superseded by 0073 — 2026-09-17. **This page is wrong.** Counting
every occurrence of the branch, rather than reading it off a first-seen
ordering, shows 453 of 454 sends transmit and exactly one abandons in a
200-second run. The idle check is not the wall; message four is never
submitted to the transmit path at all. ADR 0073 has the measurement and the
instrument defect that produced this error.

## Context

ADR 0071 established that message three is accepted and that message four never
arms the transmit slot the device models, and it inferred -- explicitly as an
inference to be confirmed -- that message four rides the hardware CCMP transmit
path. It was traced, and the inference is **wrong**. The reason is simpler and
much more tractable.

Tracing the transmit path from the moment the supplicant's message-three
handler returns success (0x4206ef00), the station's message-four send runs:

    ieee80211_set_tx_desc -> ppTxProtoProc -> ppCalFrameTimes -> ppMapTxQueue
    -> lmacIsIdle -> ppTxProtoProc+349 -> (abandons; lmacTxFrame never runs)

`ieee80211_crypto_encap` (0x4003adb4), the CCMP encapsulation, **does not run**
-- message four is plaintext, as an EAPOL message four should be. So the cipher
is not involved at all. The send is abandoned in `ppTxProtoProc` at the branch
after `lmacIsIdle`, exactly the wedge ADR 0061 first found: "every later
`ppTxProtoProc` abandoned its send without even reaching `lmacTxFrame`."

`lmacIsIdle` (0x4003b170) is four instructions of decision: it indexes the LMAC
queue table (`*0x4003b030 + index*40`), reads the state byte at offset 18, and
returns idle **only if that byte is zero**. For message four it is not zero, so
`lmacIsIdle` says busy and `ppTxProtoProc` gives up. Message four is never
transmitted, the access point never receives it, and the station leaves.

This is the same state byte ADR 0062 chased: the flash driver writes 5 or 6
there on a completed transmit and clears it to 0 afterwards. The device drives
that lifecycle only through the completion registers and interrupt it models;
somewhere between message two completing and message four being attempted, the
queue message four uses is left with a non-zero state, and nothing clears it
before the idle check.

## Decision

**Treat the wall as a transmit-completion / queue-state problem, not a cipher
problem.** The next work is to find why the queue message four is mapped to
reads non-zero at the idle check -- which queue index it is, whether a prior
transmit on that queue was completed by the device, and whether the state byte
is cleared to zero the way the repeated probe transmits clear theirs. The fix
is expected to be in the same small family as ADR 0061's one-character `|=`:
the device driving the completion state the driver reads, not a new subsystem.

The cipher work ADR 0064 set out (CCMP, ARP, DHCP) is still ahead, but it is
**not** what blocks message four, and modelling it now would fix nothing here.

## Consequences

**Good**

- The wall is far smaller than ADR 0071 feared. No CCMP hardware path, no
  encryption on the wire: message four is a plaintext frame the driver declines
  to hand to `lmacTxFrame` because it believes the queue is busy.

- It is precisely located: `lmacIsIdle` at 0x4003b170, the state byte at LMAC
  queue offset 18, and the abandon branch at ppTxProtoProc+349 (0x4003d809).

**Costs, accepted**

- **The mechanism that leaves the state byte non-zero is not yet pinned.** What
  is proven is that `lmacIsIdle` returns busy for message four's queue and that
  `ppTxProtoProc` abandons on it. Which queue, and why its state is stuck when
  the queues probes use are not, is the open question -- answerable by reading
  the queue state bytes at the moment of the idle check.

- **ADR 0071's "crypto path" reading is retracted.** It was labelled an
  inference and it did not survive the trace; this page is the correction. The
  measurements in 0071 stand -- message four does not arm the modelled slot --
  only the explanation changes.

## Alternatives rejected

**Model the CCMP transmit path.** It is not on the path: the trace shows the
send abandoned before any transmit, with the cipher never called. Building it
would be effort spent where the frame never reaches.

**Have the device force the queue idle.** The state byte lives in guest memory
and is the driver's to write; the device must drive it the honest way, through
the completion the driver processes, as it already does for the frames that do
transmit. Writing the byte from the device would be the reach-in-and-write that
ADR 0064 keeps the device away from.
