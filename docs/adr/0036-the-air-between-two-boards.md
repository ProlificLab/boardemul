# ADR 0036 — The air between two boards

**Status:** Accepted — 2026-09-07.

## Context

With the radio identified (ADR 0034) both firmwares configure it and go into
receive, and then hear nothing, for ever, because there is nothing to hear.
That is a faithful model of one node alone in a room, and it is half of what a
mesh device is. The other half is another node.

Two emulators run as two processes on one machine, so carrying a packet between
them is a matter of getting bytes from one to the other and deciding what a
board is allowed to hear.

## Decision

1. **A directory is the air.** Each board that joins writes a small file into
   it saying where it can be reached; transmitting hands the frame to everyone
   else listed. Joining is creating a file, leaving is the file going away, and
   who is on the air is something you can list.

2. **Frames go over the loopback interface.** A Unix socket would be the
   obvious choice and does not exist on the machine this is developed on, so
   the tests could not run there. The directory keeps the discovery; the
   loopback carries the bytes.

3. **It is opt-in, with `--air DIR`.** A board given no directory is a node
   with nobody in range, which is what every run so far has been and what most
   runs should stay.

4. **The radio's line follows its flags.** A host that has asked to be told
   about a packet waits on that line rather than polling, so it goes up when a
   flag is raised and comes down when the host clears it. A line that only ever
   went up would give one edge and then nothing.

5. **Nothing about propagation is modelled.** Everyone in the directory hears
   everything, at once and intact.

## Consequences

**Good**

- A packet one board sends is a packet another board receives, with its length
  and contents intact, and the receiving firmware is nudged the way the part
  would nudge it.
- The air is inspectable and needs no daemon, no configuration and no
  privileged networking.

**Costs, accepted**

- Range, collisions, interference and timing are the things a real mesh spends
  its effort on and none of them are here. A firmware whose behaviour depends
  on any of them will not be exercised, and a test that seems to prove the mesh
  works may only prove that this medium is perfect.
- A board that stops without tidying up leaves its file behind, and frames are
  sent to a port nobody is reading. That is harmless and self-correcting.

## What it took to carry a packet

Two firmwares would not talk even once the medium worked, and the reason was
not the medium. Meshtastic listens before it talks: it sweeps the channel and
waits to be told whether anything is on it. That sweep was a command this model
did not answer, so the firmware waited at it indefinitely. It was never failing
to transmit; it was still listening. Answering that the sweep finished and
found the channel clear is what let it key the radio.

Two smaller things stood behind that. The radio has to be looked at between the
guest's own accesses like the touch controller is, not on bus traffic, because
an idle guest makes almost none. And two boards booted from one captured
filesystem share an identity, so each discards the other's packets as its own;
they need distinct addresses to be distinct nodes.

With those, two emulated boards running unmodified Meshtastic exchange packets
of ninety-two and forty-eight bytes, read them off their radios, and one
reports two nodes on its own screen where it had reported one. It learned of
the other over this air and nowhere else.

## Alternatives rejected

**A multicast group.** No directory to inspect, and multicast on a loopback
inside a container is a configuration question that has nothing to do with the
board being emulated.

**A hub process the boards connect to.** Something else to start, supervise and
shut down, for a fan-out that a directory listing already gives.

**Model range and loss.** Tempting, and every number in it would be invented.
Better to have no propagation and say so than to have plausible propagation
nobody can justify.
