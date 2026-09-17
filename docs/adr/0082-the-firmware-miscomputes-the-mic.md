# ADR 0082 — The firmware miscomputes the MIC: a SHA-accelerator emulation defect

**Status:** Accepted — 2026-09-17. The root cause of the WPA2 handshake failure,
proved in one run.

## Context

ADR 0081 established that message three arrives clean and the firmware computes
a MIC that does not match, and could not reproduce the firmware's value offline.
Disassembling the MIC routine (0x42070044) and reading its inputs from guest
memory settles what it is doing.

At the call site (0x4206e187) the routine is given a key at *0x4203f500 =
0x3fca70c4 with length 16, the frame with its MIC field already zeroed, and
length 155 -- the standard HMAC-SHA1 inputs. The key read from that address is,
byte for byte, the KCK derived from the passphrase and the two nonces. So the
inputs are correct.

Breaking at the comparison (0x4206e194) and reading, in a single run, the key,
the frame and the firmware's own output:

    key (0x3fca70c4):        bed06a391ac8342df3a1048021d6a870
    received MIC:            f40a91958356b43d1ab4e21efaf112d2
    firmware computed MIC:   cfada6f650aa446ab5efe240b1212c25
    HMAC-SHA1(key, frame):   f40a91958356b43d1ab4e21efaf112d2

`HMAC-SHA1` of the firmware's own key over the firmware's own frame equals the
received MIC -- the correct answer, the one the peer sent. The firmware's own
computed MIC is something else. **The firmware miscomputes the standard HMAC
over correct inputs.** Not a wrong key, not a wrong message, not a wrong wire:
the computation itself gives the wrong answer.

The MIC runs through the ROM crypto (0x42070044 dispatches to an mbedtls-style
SHA1: init, starts, update, finish, twice, for HMAC's ipad and opad passes).
Software SHA1 executed on QEMU's CPU is exact -- it would match `hashlib` byte
for byte -- so a wrong result cannot come from correctly executed software. On
this SoC mbedtls computes SHA through the **hardware SHA accelerator**, and the
Espressif QEMU fork models that peripheral. A miscomputed HMAC over correct
inputs is an emulation defect in that peripheral.

The message-two / message-three asymmetry gives its shape. Message two, which
the firmware signs, verifies against the peer; message three, which the firmware
verifies, does not. Their inputs differ mainly in length: the HMAC inner hash
covers a 64-byte key pad plus the message, so message two spans 185 bytes and
message three 219 -- three SHA blocks against four. The defect shows at message
three's length and not message two's, which points at how the accelerator is
driven across block boundaries.

## Decision

**Treat the SHA accelerator's emulation as the fault, and pin the exact defect
next.** It is not the device model this project owns, not the Python peer, not
the key schedule, and not the frame on the wire -- all of those are now proved
correct. It is the crypto peripheral inside the QEMU fork, and the next step is
to find precisely how its output diverges: which SHA operation, at which block
count or state, produces the wrong digest, by watching the accelerator's
registers across a message-two and a message-three HMAC.

Once pinned, the fix is a carried QEMU patch under ADR 0017's rule -- the same
way ADR 0066 fixed the cross-thread TLB flush -- or, if the divergence is in how
the guest drives it under `thread=multi`, a narrower correction there.

**Keep the instruments.** `tools/dump_mic.py` and the single-run
`logs/dump_final.py` read the key, message and output together; they are how
"correct inputs, wrong output" was proved and how the accelerator-level trace
will be checked.

## Consequences

**Good**

- The whole handshake investigation resolves to one sentence: the firmware's
  HMAC is miscomputed by the emulated SHA accelerator. Every other candidate --
  the peer, the device, the key, the wire, the receive path, the state machine,
  the message four transmit -- is eliminated by measurement across ADRs 0067 to
  0081, and the remaining fault is a single peripheral.

- It is proved in one run, with the key, the message and the output all read
  from the firmware's own memory. No cross-run assumptions, no offline model of
  the frame: the firmware's own inputs, hashed, give the right answer, and the
  firmware does not.

**Costs, accepted**

- **The exact defect is not yet located.** That the accelerator miscomputes is
  proved; which operation, at which length or state, is not. It has to be found
  in the peripheral's own registers before it can be fixed.

- **The fix may be a second carried QEMU patch.** ADR 0001 keeps the C
  footprint tiny and ADR 0017 allows carried patches with an ADR each; a SHA
  peripheral fix would be the second, and larger than the TLB one. If instead
  the guest drives the accelerator wrongly only under `thread=multi`, that is a
  narrower thing, but which it is is unknown.

- **`thread=multi` is in the frame.** The handshake needs it (ADR 0066), and a
  shared accelerator clobbered across cores is one way a length-or-timing
  dependent digest error arises. Whether the defect is the peripheral model
  itself or its use under two threads is part of what pinning it must decide.

## Alternatives rejected

**Match the firmware's wrong MIC in the peer.** The firmware's value could not
be reproduced by any HMAC, CMAC, key or range (ADR 0081); it is not a
computation to match but a defect to fix. A peer that chased it would be
encoding an emulator bug into the model.

**Keep searching the message or the key.** Both are read from the firmware's
memory and proved correct here: the same key over the same message, hashed
correctly, is the received MIC. There is nothing left to vary on the input side.
