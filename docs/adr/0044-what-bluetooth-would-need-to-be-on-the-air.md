# ADR 0044 — What Bluetooth would need to be on the air

**Status:** Accepted — 2026-09-08. Records where this got to and why it stopped
there; it decides tooling, not a Bluetooth model.

## Context

LoRa went from a radio that answered to two boards holding a conversation
across a modelled medium (ADRs 0034-0039). Bluetooth's link layer has run since
ADR 0025 and transmits nothing, which is where LoRa was. The obvious next step
was to do for it what was done there: find the packet, put it on the air, let a
second station hear it.

Two things were in the way before any of that could be judged, and both are now
gone.

**Getting Bluetooth on took minutes of driving the interface.** It is switched
on by holding the boot logo, which enables it and reboots (ADR 0026), so every
experiment paid for a UI sequence before it began. `make_configured_fs.py
--bluetooth` now does that once and keeps the filesystem, exactly as the region
is kept for the mesh. A run given that overlay comes up with Bluetooth already
on, and the link layer starts within the first few seconds of guest time.

**Nothing was known about the events.** The core logged the first word of an
exchange-table entry; it now logs all sixteen bytes.

## What is true now

With Bluetooth on from boot, the scheduling side works and works hard: **2111
advertising events kicked and executed in a single run**, each one an entry the
controller wrote, timed, and had run at its half-slot. The entries sit in
internal RAM around `0x3fcca548` — not in the window this project claims at
`0x3fc00000` for exchange memory, which is why that window stayed empty and
looked like nothing was happening.

The advertising payload exists, and is exactly what it should be. In RAM there
is one copy of it, on the host stack's heap: a complete local name structure
(`10 09 "Meshtastic_0001"`), a flags structure (`02 01 06`), a sixteen-bit
service UUID and a hundred-and-twenty-eight-bit one, each with the standard
length-and-type header, laid out as a host prepares advertising data.

**And that is the finding: there is exactly one copy.** Nothing resembling it
appears anywhere in the exchange memory the controller schedules its events
from. The window those events live in holds twenty-two thousand non-zero bytes
and not one occurrence of the advertised name or of the flags structure. The
controller schedules the events and never assembles a packet for them.

## Decision

1. **Keep the two things that made this measurable**: the `--bluetooth`
   filesystem, and `tools/find_ble_advertising.py`, which dumps a running
   guest's RAM through the monitor and reports where the advertising data is
   and where it is not.

2. **Emit only what the controller points at.** Taking the payload off the
   host's heap because it happens to be there would be inventing a packet; but
   reading the descriptor the controller wrote, resolving its offsets through
   the base registers the controller programmed, and emitting the bytes that
   land is reading what the hardware staged. The first is theatre and the
   second is a model. That distinction is the whole of this record.

3. **What remains is the medium, not the packet.** The packet is in hand. What
   Bluetooth still lacks is what LoRa got in ADRs 0036-0039: something to carry
   it, and a second station to hear it.

## How far that was chased

Far enough to name the step rather than the area.

The ROM function that stages advertising data is `r_lld_adv_adv_data_set`
(stub 0x40003fb4, code 0x40016954), and it **runs**: filtered tracing shows the
blocks past its early-out and the store itself translated, so it is not bailing
out.

It finds where to write by calling `r_emi_get_mem_addr_by_offset` (0x4000a360)
with an exchange-memory offset. That translation is now decoded: the page is
`offset >> 10`, the register serving it comes from a ROM table at 0x3ff1f526,
and **the register itself is at 0x60031204 + index * 4** -- the block this
project already answers. The ROM then checks `(reg >> 18) << 2` against the
page it asked for, which is the readback assert ADR 0023 met from the other
side, and computes `0x3fc00000 | ((reg & 0x3ffff) << 2)` plus the offset within
the page.

Logging those mappings shows the controller making about forty-eight hundred of
them in a run, and they are sane: the exchange table is at 0x3fcca548, and the
page holding the advertising offset maps to 0x3fccae38, where a descriptor is
duly written. Twenty-five pages are mapped with a zero address, which resolves
to 0x3fc00000 and is worth doubting later.

### And then the answer: nothing copies them

The conclusion above, that the payload never reaches exchange memory, was
wrong, and wrong in an instructive way. It came from looking for a *second
copy* of the bytes and not finding one.

Following every offset in the advertising descriptor, rather than the first
two, resolves it. The record at 0x1400 holds two more: `0x2400` and `0x2c00`.
Resolved through the same base registers, they land on
`10 09 "Meshtastic..."` and `02 01 06 03 03 0f 18 11 07 ...` -- the name
structure and the flags with the service UUIDs. They are the advertising data,
reachable from the descriptor, at the moment an event runs.

And their addresses are the heap addresses the search had already found.

So nothing copies the bytes. **The controller maps the page the host's buffer
already sits in into exchange memory and refers to it by offset.** The base
registers do the work that a copy would otherwise do, which is exactly why
there is one copy of the payload in RAM and why looking for a second one found
nothing. The single copy was not evidence of a missing step; it was the
mechanism.

That also means the payload is legitimately available. Emitting it would not be
reading a heap block the emulator has no business touching: it would be reading
what the controller staged, through the controller's own mapping, at the moment
the controller scheduled a transmission -- the same standard the radio path is
held to.

## Consequences

**Good**

- An experiment now costs one run with Bluetooth already on, rather than a UI
  sequence and a reboot first.
- The question changed from "does advertising work" to a specific one about a
  specific step, and the answer to the first is yes, two thousand times a run.

**Costs, accepted**

- Bluetooth still transmits nothing: the packet can be read, and nothing yet
  carries it.
- The `bt_em` window at `0x3fc00000` is now known not to be where the
  controller's tables live. It is left alone rather than removed: it was
  claimed because the guest faulted there (ADR 0023) and that reason still
  holds, but it should not be mistaken for exchange memory.

## Alternatives rejected

**Emit the host's advertising data when an event executes.** The quickest way
to two boards seeing each other, and it would be theatre: the payload would
come from a heap block the emulator has no business reading, on a schedule the
controller happens to keep. The first firmware that advertised something
different, or stopped, would go on working.

**Model the baseband properly first.** Correct and large. Naming the step is
what this record is for; doing it is its own piece of work.
