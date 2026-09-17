# ADR 0074 — The four-way handshake completes, and the station leaves anyway

**Status:** Accepted — 2026-09-17. Amends 0073's central measurement.

## Context

ADR 0073 measured that message four is never submitted to the transmit path,
and concluded the question was inside the supplicant. The measurement was
sound for the run it was taken on. It is **not a property of the firmware**.

Running the same firmware under the wide trace filter, which slows the guest
considerably -- `BOARDEMUL_TCG_THREADS=multi`, `BOARDEMUL_QEMU_DFLAGS=exec`
and

    -dfilter 0x4003a000+0x4a00,0x40040b00+0x600,0x420c0000+0x18000,0x42060000+0x20000

-- the peer reports, twice, in two separate runs:

| run | filter | assoc | eapol 1 | eapol 2 | eapol 3 | **eapol 4** | **done** |
|---|---|---|---|---|---|---|---|
| A | wide | 3 | 3 | 3 | 3 | **2** | **2** |
| B | wide | 4 | 4 | 4 | 4 | **1** | **1** |
| C | narrow | 5 | 5 | 5 | 5 | 0 | 0 |

**The WPA2 four-way handshake completes.** Message four arrives, its integrity
code verifies against the key schedule derived from the passphrase, and the
peer declares the handshake done -- against unmodified vendor firmware. Every
part of ADR 0064's crypto goal up to and including the handshake is now proved
on the wire rather than offline: PMK, PTK, KCK, KEK, the MIC in both
directions, the AES key wrap of the GTK, the RSN element, and the ordering
rules for addresses and nonces.

So message four is not blocked by anything structural. It is **timing**: the
narrow-filter run is fast in guest time, and the peer -- which answers on wall
time -- lands its reply later in guest time than the station will wait. Slow
the guest and the reply arrives in time. Three runs is enough to show the
handshake completes and not enough to call the mechanism proved, so it is
recorded as the leading explanation, with the correlation stated plainly.

**The new wall is one second wide.** For the attempt that completed:

    161942 ms  associated; handshake armed
    161944 ms  message one goes to the station
       ...     message two, three, four -- the handshake completes
    162913 ms  disassociated: reason 8, the station is leaving the network

The station finishes the handshake and leaves about **970 ms** later. It does
not ask for an address: across the whole run the peer received 5 data frames
and they are exactly the 4 message twos and the 1 message four. The device
counted 426 arms and **zero** unserviced-slot arms, and ADR 0073's accounting
showed arms and received frames match one for one, so the station armed **no
data frame at all** after message four. It never attempts DHCP; it gives up.

Between sending message four and using the link, a station installs the
pairwise and group keys into the MAC's key slots. Those registers are in a
window the device claims and does not model, so the writes are accepted and
forgotten. That is the first thing to look at, and it is where ADR 0071's
instinct about the cipher belongs -- not on message four's transmit, which is
plaintext and now known to be sent.

## Decision

**Treat the handshake as done, and make the post-handshake key installation
the next subject.** Find what the driver writes to the MAC after message four,
with a verbose bus log over that one-second window, and model the crypto
control interface that answers it -- per the approach already recorded: both
ends of the air are ours, so the cipher can be identity, and only the control
interface the driver checks has to be real.

**Do not treat the trace filter as the fix.** It is scaffolding that happens to
slow the guest into a working timing. The handshake completing only under a
debug filter is not a working emulator, so the peer's reply latency in guest
time is a defect to be measured and closed on its own.

## Consequences

**Good**

- **The handshake completes against real vendor firmware.** This was the wall
  from ADR 0058 through 0073 -- eight ADRs and several sessions -- and it is
  now measured as passed, twice, not inferred.

- The next wall is precisely bounded: one second of guest time, a known
  starting event (message four went out) and a known ending event (reason 8),
  with the station's own silence as the symptom.

- ADR 0073's instrument and arithmetic still earn their keep: it is exactly
  the arms-equal-frames accounting that turns "the peer saw nothing" into "the
  station armed nothing", which is a much stronger statement.

**Costs, accepted**

- **The handshake completes only when the guest is slowed by tracing.** It is
  not yet reproducible in an ordinary run, so no test can assert it. Until the
  reply latency is closed, `tests/test_wifi_connect.py` stays at association.

- **The timing explanation is the leading one, not a proved one.** One
  narrow-filter run against two wide ones is a correlation. The alternative --
  that some retry ordering rather than speed is what differs -- has not been
  excluded.

- **Message two's integrity code still fails once or twice per run**
  ("message two, mic did not verify"), even though ADR 0070 made it reproduce
  exactly. A stale nonce from a previous attempt is the obvious candidate and
  it is unexamined.

## Alternatives rejected

**Chase the supplicant disassembly ADR 0073 pointed at.** There is nothing
wrong there: the supplicant builds and sends message four. The disassembly
would have been reading correct code looking for a defect in it.

**Assert the handshake in a test now.** It passes only under a debug trace
filter. A test that needs `-d exec` to pass is measuring the instrument.

**Model real CCMP for the data path.** Still unnecessary, and still for the
reason ADR 0073 gave: both ends of the air are ours. What is needed is the
control interface the key install talks to, not the cipher behind it.
