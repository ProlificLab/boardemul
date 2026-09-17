# ADR 0077 — There are no hardware key slots to model; the crypto is software

**Status:** Accepted — 2026-09-17. Corrects the "key-install registers located"
finding in ADR 0076's addendum, which was wrong for the third time in the same
way.

## Context

ADR 0076's addendum reported that a cluster of registers -- 0x12f8, 0x12fc,
0x1310, 0x1314, 0x1318 -- was the key-install control interface, because a
completing handshake touched them and a failing one did not. The instruction
that followed was to model them as key slots. Looking properly, before writing
any model, three things are true and each on its own kills that plan.

**They are not key slots. They are per-transmit MAC configuration.** The ROM
site that writes 0x1318 (pc 0x4003b6e9) is inside `mac_tx_set_duration`
(0x4003b6b8); the site that writes 0x1314 (0x4003b6ac) is the function beside
it. The cluster is written 8910 times in one run -- at boot, during scanning,
and on every transmit -- with 0x3b the value on two hundred of the writes.
This is the rate-and-duration setup a frame gets before it goes on the air,
not anything to do with keys.

**The completing-window finding was the same instrument artifact a third
time.** A window that completes the handshake runs more transmits than one that
stalls, so it runs `mac_tx_set_duration` more often; the cluster appeared "only
in success" for the same reason ADR 0073's first-seen ordering and ADR 0072's
mislabelled offsets did -- more code ran, not different code. The register diff
compared quantity and read it as kind.

**The key never touches the device's MMIO at all.** The pairwise key for the
completing handshake was computed from the wire (TK 28f3ba5d c71bdce1 385415a7
0acfbaa9) and searched for in every register write of the run, in both byte
orders. It does not appear. The other MAC sub-blocks the board claims,
wifi_mac3 (0x60036000) and wifi_mac4 (0x60037000), are not accessed in the run.
The ROM carries `ieee80211_crypto_encap` (0x4003adb4) and
`ieee80211_crypto_decap` (0x4003ae18): this firmware does CCMP in software, in
the CPU, with the key held in DRAM. There is no hardware key slot to fill.

## Decision

**Do not model key slots. There are none.** The device's register window is
already correct for this cluster -- plain storage, which the driver's own
read-modify-write depends on (it writes 0x1000, reads 0x1000, writes 0x1100).
Modelling it as anything else would break the readback the driver relies on.

**The real wall is upstream of any crypto, and it is not about keys.** The
completing attempt is unambiguous:

    207929 ms  the station sends message four -- the handshake completes
       ...     5.6 seconds, zero data frames armed
    213531 ms  the station sends a disassociation, reason 8, and leaves

The station finishes the four-way handshake and then transmits nothing at all
before it leaves. It never encrypts a data frame, so software CCMP is not yet
on the path either. The question is why a station that has just completed WPA2
does not proceed -- post its connected event, start DHCP -- and instead tears
the association down itself. That is the next subject, and it is to be
investigated, not guessed.

## Consequences

**Good**

- No effort spent modelling a hardware key interface this firmware does not
  use. When the encrypted data path is finally on the air, the crypto belongs
  in the Python peer (both ends of the air are ours; ADR 0064), matched to the
  software CCMP the ROM does -- `emu/aes.py` already has CCM.

- The failure is now located precisely in time and ruled clear of the device:
  the station's own decision to leave, 5.6 s after a successful handshake,
  having armed nothing.

**Costs, accepted**

- **A fourth ADR about this wall, correcting a third wrong reading.** The cost
  of writing decisions before code is that the wrong turns are on the record
  too. The instrument that produced all three -- counting how much code ran in
  a window and reading it as which code -- is now named often enough that it
  should be the first suspicion next time.

- **Why the station leaves is still open.** Reason 8 is "the station is
  leaving"; what convinces it to, after a handshake it completed, is unknown.

## Alternatives rejected

**Model 0x12f8 as a key slot, per the instruction.** The instruction rested on
ADR 0076's finding, and the finding was wrong: 0x12f8 is transmit duration, and
the key is never written to MMIO. Building the model would have been writing
code against a register whose meaning was misread.

**Model software CCMP in the peer now.** The station arms no data frame after
the handshake, so there is nothing encrypted to decrypt yet. The crypto is
needed only once the station actually sends past the handshake, which it does
not do until the wall above is understood.
