# ADR 0030 — The touch controller reports in its own raster

**Status:** Accepted — 2026-09-04.

## Context

The GT911 model reported display coordinates: a tap meant for the pixel at
(277, 117) was handed to the firmware as (277, 117), as though the controller
shared the interface's landscape frame. Nothing had contradicted that. The
vendor firmware on this board has never been seen polling touch, and the
model's own tests only asked whether a coordinate survived the wire.

ADR 0029 then got Meshtastic reading the controller, and it still could not be
operated: four hundred and thirty reports read in a run, and not one reaching a
control. Meshtastic reads touch through LovyanGFX, which applies a calibration
its driver carries for this board, four corner points measured on real
hardware: `{0,2, 0,314, 223,5, 224,314}`. Under that transform our coordinates
left the widgets entirely.

Those points pair up: two share a raw x, two share a raw y. The pair sharing a
raw x is a row of the screen, the pair sharing a raw y is a column. So the
controller's x axis runs along the screen's short side and its y along its
long one, spanning about 224 by 314 — within a few per cent of one to one
against a 240x320 raster. That is the portrait raster the ST7789 behind it has
(`NATIVE_W, NATIVE_H` in `lcd.py`), which is what two parts mounted behind one
piece of glass should look like.

What the constants do *not* settle is which way up each axis runs, because the
order of the four points is a convention of whoever recorded them. Guessing
cost two runs, and the guess was hidden by the obvious test: the short-name
field's keyboard icon straddles the middle of the screen, so it is hit whether
the axis is read upright or inverted. The fields further down are what
discriminate, and they missed. Tapping one column at two rows — the row that
reaches the long-name icon if the axis is upright, and the mirrored row — the
mirrored row is the one that raised the keyboard. The controller counts *up*
the screen.

## Decision

1. **The controller reports in its own raster**, 240x320, and advertises that
   in its resolution registers. It knows nothing of the mounting.

2. **The board states the mounting.** `touch_point()` in `board_tdeck.py` maps
   a display coordinate to a controller one: the screen column becomes the
   controller's y, and the screen row becomes its x counted from the bottom.

3. **The active area is modelled, not only the turn.** The controller reports
   within an inset of its raster, and the mapping spans the measured edges. A
   mapping over the full raster leaves targets drifting by twelve to fourteen
   pixels, enough to miss a button twenty-four pixels tall.

4. **The axis directions are what the device did**, not what the constants
   looked like they meant. Anything similar in future gets the same treatment:
   probe a target that discriminates, and never one that straddles the middle.

## Consequences

**Good**

- A tap lands where the host aimed it. Meshtastic's setup dialog is operable
  by touch: tapping a field's keyboard icon binds the keypad to that field,
  typed characters land in it, and OK commits.
- The two parts behind the glass are now described the same way, both portrait,
  with the turn stated by whoever knows the mounting.

**Costs, accepted**

- The edge constants come from one firmware's fitted calibration, the only
  observation available. The inset is a property of the part, but it is
  measured through one driver, and a second firmware could refine it.
- A firmware that assumed the controller shares the display's frame would now
  be handed different numbers. No such firmware has been observed on this
  board; one that did would be misreading the part.

## Alternatives rejected

**Keep display coordinates and transform per firmware.** The model would have
to know which firmware is running, which is the thing ADR 0006 exists to avoid.

**A turn with no inset.** Honest about the mounting, simpler, and it misses
controls by more than half a button.

**Invert the calibration matrix exactly.** Arithmetically similar, but framed
as copying one driver's fit rather than describing the part, and it would carry
that driver's per-corner noise into the model — including the ambiguity of
point order that cost two runs here.
