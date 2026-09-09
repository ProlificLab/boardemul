# ADR 0054 — What WiFi would need to hear something

**Status:** Accepted — 2026-09-09. Names the work; does not do it.

## Context

With the backlight modelled (ADR 0053), WiFi is the only window any firmware
here touches that has nothing behind it -- and by a wide margin: half a million
accesses to the front end alone. It has also been described wrongly in this
repository for some time. The board file says the firmware "writes a little
config here and then polls +0x050 about a thousand times a second, forever,
inside `esp_wifi_start()`". That was true once and is not now.

**What it actually does.** Measured on the current build:

    [WiFi-event] event: 0    WiFi interface ready
    [WiFi-event] event: 2    WiFi client started
    [WiFi-event] event: 5    Disconnected from WiFi access point   (repeatedly)

The driver starts, brings the station up, and scans. Of the seventy-two
thousand register accesses in a two-and-a-half minute run, twenty-one thousand
are a pair -- `fe +0xc8` taking an index and `fe +0xcc` the value -- which is a
calibration table being loaded, not a spin; and five hundred and eighty-four
matched operations across `nrx +0x1c`, `nrx +0x34`, `bb +0x14`, `bb +0x18` and
`wifi_mac0 +0x400` are channel changes, which is a scan hopping. Nothing is
stuck. The firmware is looking, and there is nothing to find.

**Where a frame would go.** The driver writes exactly one internal-RAM address
into the MAC's registers while starting: `wifi_mac0 +0x88` gets `0x3fcb6b0c`.
Read on a running guest, that is a chain:

    0x3fcb6b0c   808a48a4  3fcb6b4c  3fcb6b18
    0x3fcb6b18   808a48a4  3fcb7404  3fcb6b24
    0x3fcb6b24   808a48a4  3fcb7cbc  3fcb6b30
    0x3fcb6b30   808a48a4  3fcb8574  00000000

Four descriptors, twelve bytes each: a word of flags and sizes, a buffer, and
the next descriptor, with the last one ending the chain. Each buffer holds
`0xdeadbeef` -- the allocator's poison, untouched, because nothing has ever
been received into it. The flags word is the same on all four, and its top bit
is set, which on every descriptor of this shape means the hardware owns it and
is waiting.

That is the same shape the Bluetooth receive path turned out to have (ADR
0046): a ring the controller is handed, and a model that fills the one the
firmware will read next.

## Decision

1. **Say what it is, and stop there.** The comment claiming a poll at `+0x050`
   is removed, because a wrong description of where something stops is worse
   than none -- it sends the next investigation to the wrong register.

2. **Record the ring rather than build on it.** Three things are known: which
   register carries it, how the descriptors are linked, and how large the
   buffers are. Three are not: which bits of the flags word are the length the
   hardware fills in, which interrupt says a frame arrived, and what the
   driver does with a descriptor it has been given. None of those can be read
   off a run, because no frame has ever arrived to make the driver act.

3. **It will need a device in C.** Delivering a frame means writing into guest
   memory at a moment of the model's choosing and raising an interrupt, which a
   register-level Python model cannot do -- the same reason the Bluetooth core
   is in C (ADR 0025). That is a new file rather than scattered edits, so it
   stays inside ADR 0001, but it is not a small one.

## Consequences

**Good**

- The next attempt starts from a located ring instead of from the belief that
  the driver is stuck in `esp_wifi_start()`.
- The measurement that found it is cheap and repeatable: scan a run's writes to
  the WiFi windows for a value that looks like an internal-RAM address, and
  there is exactly one.

**Costs, accepted**

- **WiFi finds no networks, and that is the whole of the gap.** Everything else
  a board has is modelled: every I2C address any firmware talks to, every chip
  select, both I2S engines, the radio, the card, Bluetooth, the receiver on the
  second UART, and the backlight.
- The descriptor's flags word is recorded as one observed constant. Reading it
  properly means disassembling the controller library's own interrupt handler,
  which is in flash rather than ROM and has no names to go on.
- A scan finding an access point is only the first thing WiFi could do. A
  connection, and then traffic, is more again -- and would want an air of its
  own, as the radio and Bluetooth each have.

## Alternatives rejected

**Fill a descriptor with a beacon and see what happens.** Without knowing which
bits carry the length or which interrupt to raise, this is guessing at a
structure the firmware will act on, which is the failure mode ADRs 0048 and
0050 already cost three documents to.

**Leave the description as it was.** It named a register the firmware no longer
polls and a function it no longer stops in, and it would have cost the next
attempt a day.
