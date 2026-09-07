# ADR 0033 — A card in the slot

**Status:** Accepted — 2026-09-07.

## Context

With the bus routing by chip select (ADR 0032) the card's select is no longer
swallowing its traffic, and the first thing recorded on it was
`40 00 00 00 00 95`: CMD0, GO_IDLE_STATE, with the CRC every SD host sends for
it. The firmware has been trying to talk to a card all along and hearing
nothing back. Both builds report the same failure, `f_mount failed: (3) The
physical drive cannot work`, and the vendor build spends its start-up retrying.

A card that answers is not enough on its own. A host that gets through
initialisation then reads sector zero and expects a filesystem; a card holding
zeros initialises fine and fails to mount, which looks identical from the UART.
So the decision is really two: what the card answers, and what is on it.

## Decision

1. **The card is an SDHC card in SPI mode**, which is what the firmwares'
   hosts negotiate anyway and what makes addressing simple: CMD17 and CMD24
   take a block number, not a byte offset, so nothing has to scale.

2. **It is modelled as a byte stream, not a transaction machine.** The host
   clocks one byte and gets one byte, and a command is recognised by its own
   framing: a byte with its top two bits `01` starts six. This is what the
   wire shows, and it avoids depending on where a chip-select window happens
   to be cut, which for this controller varies with how the driver batches.

3. **The image is built here, not shipped.** A FAT16 volume behind an MBR
   partition, made by `emu/sdimage.py` with the standard library alone, so
   there is no binary blob in the tree whose contents nobody can read.

4. **Writes land in memory and are dropped at the end of a run.** A run starts
   from the same card every time, which is what makes a boot reproducible.
   Persisting them is a later decision and needs a reason.

## Consequences

**Good**

- Both firmwares mount storage instead of retrying and giving up, and the
  vendor build gets its start-up seconds back.
- The card's contents are described by code, so a test can put a file on it
  and then assert the firmware found it.

**Costs, accepted**

- A byte-stream model cannot notice a host that abandons a command mid-frame;
  it would resynchronise on the next byte that looks like a command. No host
  seen does that, and the alternative costs a protocol change to carry
  chip-select edges.
- FAT16 rather than FAT32: smaller and simpler to lay out by hand, and it caps
  the card at the size where FAT16 stops being sensible. Nothing needs a large
  card yet.
- The CRC a host sends on a command is accepted without being checked, but the
  one the card sends on a data block is computed properly. The vendor firmware
  turns checking on with CMD59 as its second command, so a faked value would
  read to it as a broken card.

## Alternatives rejected

**Ship a pre-made image.** A binary in the tree that nobody can regenerate or
inspect, and the first question about any mount failure would be whether the
image was right.

**Model the card at the transaction level, driven by chip-select edges.** It
needs the controller to send those edges, which is a protocol change, and it
buys accuracy against a failure mode no host exhibits.

**Persist writes to the image file.** Convenient for poking at what a firmware
wrote, and it makes two runs of the same test differ, which is exactly what
this project spent a long time removing from its Bluetooth work.
