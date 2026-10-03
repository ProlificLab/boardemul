# ADR 0126 — The T-Deck skin is drawn from LilyGO's own sources

**Status:** Accepted — 2026-10-03. Replaces the T-Deck's drawing in
`emu/app.py`, which was the T-Deck Max's with other numbers.

## Context

`emu/app.py --board tdeck` drew the Max's body for the T-Deck:

- a rounded slab with a speaker grille, a status LED and side buttons;
- "T-DECK MAX" printed on it;
- the trackball beside the keyboard, between two buttons;
- caps labelled ALT, DEL, ENT, SYM and UP;
- a bottom row of SYM, 0, a six-column space bar and UP.

None of that is the T-Deck. It does not look like the device, and the bottom
row sends keys in places the device does not have them.

LilyGO publishes enough to draw the real one:

- **The outline.** `shell/T-Deck.dxf` in Xinyuan-LilyGO/T-Deck is the main
  board's drawing (`logs/ref/tdeck/`). The keep-out layer and one cubic
  B-spline give the board outline: 67.06 × 98.94 mm, square-shouldered with
  0.5 mm corners, straight sides, and a rounded bottom from 84.7 mm down. The
  uncased T-Deck's front is its boards, so this is the device's silhouette.
  The trackball's ring is centred 54.4 mm from the top (circles Ø15.39 and
  Ø20.26).
- **The panel.** The product page says 2.8 inches at 320 × 240. That makes the
  active area 57.6 × 43.2 mm, a 0.18 mm pixel.
- **The face.** LilyGO's product photographs (lilygo.cc, the T-Deck page),
  read against the drawing's scale, show what sits where:
  - the panel's black glass across the top, to about 46 mm;
  - a black strip to about 59 mm, with three light chevrons and a thin line on
    each side of a chrome-ringed trackball;
  - four rows of charcoal caps at a 7.7 mm pitch, ten columns at 6.2 mm, on a
    translucent grey plate;
  - a black band below them with "LILYGO®" between two arrowed lines.
- **The keys.** The keyboard co-processor's own sketch
  (`examples/Keyboard_ESP32C3`) lists all 35 keys and each one's second
  character: `#` on Q, `1` on W, `@` on P, `0` on the microphone key, and so
  on. The photographs print the same characters above the letters. The bottom
  row is left shift, microphone/0, a space bar three columns wide, sym, right
  shift. Enter, backspace, shift, the microphone and `$`'s speaker are printed
  as icons, not words.

Tk's canvas has no anti-aliasing for shapes, no gradients and no alpha. The
old drawing worked around that with stippled shadows, and at this size a
rounded outline stair-steps.

## Decision

1. **The T-Deck's face is laid out in millimetres from those sources**
   (`emu/tdeck_face.py`). Each number cites the drawing, the specification,
   the sketch, or the photograph it was read from. It is converted at 50/9
   pixels per millimetre, which makes the panel exactly 320 × 240 at scale 1.
   The panel's origin is rounded to a whole pixel at every scale, so the
   screen stays pixel-exact.

2. **The body is rendered, not drawn with canvas shapes.** A small
   anti-aliased rasteriser in the standard library (`emu/raster.py`) fills
   convex shapes with exact coverage at their edges, and fills gradients per
   row. The body, glass, strip, plate, caps, trackball, chevrons and icons are
   painted into one image and shown as a Tk photo. That is about a megapixel
   at scale 2, rendered once at start-up.

3. **Legends are Tk text over the image**, so they are drawn by the system's
   anti-aliased font renderer: a letter, its second character above it, and
   `alt` and `sym` in lower case as printed. Icons are rendered shapes, so
   they look the same on any machine.

4. **The keys are the vendor's 35 and send the emulator their names.** Letters,
   `$`, `SPACE`, `ENTER`, `DEL`, `ALT`, `SYM`, `LSHIFT`, `RSHIFT` and `MIC`.
   The keyboard model accepts the four modifiers as keys the board has. What
   they do inside the keyboard is the subject of a later decision.

5. **No photograph is bundled.** LilyGO's pictures are references for
   proportion and colour, not assets. The repository stays free of binaries
   nobody can regenerate, as ADR 0033 and the README already hold.

6. **The Max keeps its drawing for now.** It gets the same treatment from its
   own sources separately.

## Consequences

**Good**

- The window looks like a T-Deck: its outline, its face, its keyboard with the
  characters printed on the real caps, and its trackball where the thumb
  finds it.
- Every drawn key is a key the device has, in the place it has it.

**Costs, accepted**

- Positions read from photographs are good to a few tenths of a millimetre,
  not to the drawing's hundredths. Each is marked as photographic.
- Start-up renders the body in Python, about half a second at scale 2.
- The drawing is not quite square: its bottom spline starts 0.13 mm right
  of the edge its width is dimensioned from. The outline keeps the spline
  as drawn, which is less than a pixel at scale 1.
- The board seen through the translucent plate, the screws and the side
  connectors are not drawn.
