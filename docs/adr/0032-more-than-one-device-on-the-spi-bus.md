# ADR 0032 — More than one device on the SPI bus

**Status:** Accepted — 2026-09-07.

## Context

SPI2 on this board carries three devices: the display on chip select 12, the
LoRa radio on 9, and the microSD card on 39. `board_tdeck.py` has named all
three pins for a long time; only the display has ever been modelled.

The controller model forwards a segment only while its one configured chip
select is asserted and silently drops everything else, so the other two devices
are not merely unmodelled, they are unreachable. Both firmwares show it. The
vendor build prints `Start Radio failed,code:-2` and fails to mount a card;
Meshtastic, whose entire purpose is the radio, does the same.

There is a second obstacle in the way, and it is the reason this cannot be a
one-line change. The controller has a single `miso` switch for the whole bus,
off on this board because the display drives nothing back. With it off the
guest reads whatever was left in the transmit FIFO, and that is where the
vendor firmware's `Init display id:210` comes from. A card and a radio both
reply, so the switch has to exist per device: turning it on for the whole bus
would start handing the display zeros and change behaviour that works today.

## Decision

1. **The controller routes by chip select.** Up to three chip-select pins; the
   asserted one picks the device. Its index travels in the byte the segment
   header already reserves as padding, so the wire format keeps its size and
   its meaning stays declared in one place on each side.

2. **Which devices drive MISO is a mask, not a bus-wide switch.** One bit per
   device index. The display's bit stays clear, so nothing about the display
   changes, and a device that does reply gets its reply forwarded.

3. **Nothing selected means nothing forwarded**, exactly as now. A bus with no
   chip select asserted is idle, not broadcasting.

4. **The Python side takes a device per chip select**, and a single device with
   no index keeps working, because most boards have one.

## Consequences

**Good**

- The radio and the card become modellable, which is the only route to a
  Meshtastic that does anything a mesh device does.
- The display is untouched: same routing, same MISO behaviour, same id.
- The wire format's size is unchanged, so the two sides stay in step.

**Costs, accepted**

- Three chip selects is a fixed ceiling, chosen because it is what this board
  has. A fourth device would mean revisiting it.
- The mask is another knob on a device that had one. It is the honest shape:
  whether a part drives a shared line is a fact about the part.

## Alternatives rejected

**One controller instance per device.** Each would need its own socket and its
own copy of the controller's registers, and they would fight over them. The
controller is one peripheral with one register file; only the bus is shared.

**Turn MISO on for the whole bus.** One line instead of a mask, and it changes
what the display returns from stale FIFO content to zeros, losing the id the
vendor firmware reads and prints. A change that breaks a working device to
enable a new one is not a simplification.

**Give each device its own socket and let the board demultiplex.** Moves the
routing out of the place that knows which chip select is asserted, which is the
controller. The board would have to infer it from GPIO levels it does not see.
