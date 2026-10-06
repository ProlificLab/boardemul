# ADR 0130 — The Max's face is drawn from LilyGO's own case

**Status:** Accepted — 2026-10-06. Replaces the Max's drawing in `emu/app.py`,
which was invented: a trackball the Max does not have, a four-key bottom row
where the case has five keys, a speaker grille, a status LED and "T-DECK MAX"
printed on a slab. ADR 0126 gave the T-Deck its own face and left "the Max
keeps its drawing for now"; this is that, closed.

## Context

LilyGO publishes the Max's case as STL models, and the panel has a datasheet:

- **The case.** `T-Deck-MAX-Case-Files` (LilyGO's product repository), kept in
  `logs/ref/tdeck-max/`. The front is `surface-covre.stl`: a 120.12 × 65.81 mm
  outline whose top corners round at 6.8 mm and whose bottom rounds at 28 mm,
  a glass recess 63.88 × 79.38 mm (corner radii 5.9 top, 5.7 bottom) running
  from 1 mm below the top edge, an 8 mm rounded cut through the top-right
  corner, and 35 key openings: ten columns at a 6.24 mm pitch, three rows of
  6.3 mm-tall caps, and a bottom row of five — two keys, a 17.4 mm space bar
  spanning the middle, then two keys, the row set to its own spacing, off
  the columns, and closer to the logo band. `middle-bezel.stl` and `key.stl` sit
  behind the front and only the front is drawn.
- **The panel.** The GDEQ031T10 datasheet: a 3.1-inch e-paper whose active
  area is 47.04 × 62.72 mm at a 0.196 mm square pixel — 240 × 320 exactly.
- **The photographs.** LilyGO's product photographs, read against the STL's
  scale: which way round the model is (the corner cut is on the device's top
  right), the light grey touch glass filling the recess, three touch keys
  printed along the glass below the active area — a heart, a speech bubble
  and a paper plane, spread wide rather than centred — and the e-paper's
  content ending some 6 mm above the recess's bottom edge, which places the
  active area's bottom edge. The keyboard's legends print the same characters
  as the T-Deck's, and the bottom band carries "LILYGO®" between marks.

The vendor's matrix (ADR 0126's `peri_keypad.cpp`, the TCA8418 model) reads
the bottom row as `UP, 0, SPACE, SYM, UP2` in matrix-column order. Read with
the photographs' notch, the matrix columns run left to right, so the Max's
bottom row is two up keys flanking the left of the space bar, `0`/microphone,
`sym` — the same physical keyboard as the T-Deck with both shift positions
wired as up keys. The old drawing had `SYM, 0, SPACE, UP` and only four.

The three touch keys under the screen are touch keys, reported by the touch
controller as discrete key events rather than touch points. They have no
model yet (ADR 0129 left them out too); this decision draws them, and their
modelling stays open. *(Closed by ADR 0131, which models them.)*

## Decision

1. **The Max's face is laid out in millimetres from those sources**
   (`emu/max_face.py`), in the shape ADR 0126 set: each number cites the STL,
   the datasheet or the photograph it was read from, and the shared cap,
   legend and icon machinery lives in `emu/face.py`, factored out of the
   T-Deck's face so neither board carries a private copy.

2. **It is converted at 250/49 pixels per millimetre** — the reciprocal of
   the panel's 0.196 mm pixel — so the active area is exactly 240 × 320 at
   scale 1 and the panel's origin is rounded to a whole pixel at every scale.

3. **The outline is a span, not a polygon.** The corner cut makes the Max's
   outline concave and the rasteriser fills convex shapes; the body is
   therefore an interval function — a rounded rectangle whose right edge is
   the cut's own curved wall on the rows the cut spans — which stays a single
   interval because the cut bites the edge. The shadow and the rim are the
   same span offset and inset.

4. **The keys are the case's 35 and send the matrix's names.** The two up
   keys are drawn as `UP` and `UP2`, which the TCA8418 model has; the old
   test's "only one UP is drawn" exception is closed by the Max's own tests.

5. **The three touch keys are drawn, not modelled.** Clicking them hits the
   glass below the active area, which is not the panel and is not a key, so
   the click does nothing — the honest state of a board whose touch-key
   model does not exist yet.

6. **No photograph is bundled**, as ADR 0126 held.

## Consequences

**Good**

- The default window looks like a T-Deck Max: its outline with the corner
  cut, its grey glass, its five-key bottom row, and no trackball.
- Every drawn key is a key the TCA8418 has, including both up keys.

**Costs, accepted**

- The active area's position is photographic, good to about a millimetre:
  the datasheet sizes it and the photographs place its bottom edge. A
  measurement of the assembled device would tighten it.
- The key legends are read as "the same as the T-Deck's" from photographs
  that do not resolve every cap. Each cap's second character cites the
  T-Deck's sketch by way of that reading, not the Max's own printing.
- What sits inside the corner cut is drawn as a recess with a metal ring
  from the photographs' hint; the STL says the cut, not its contents.
- The touch keys' heart, speech bubble and paper plane are drawn as shapes
  from the photographs' silhouettes, not traced from them.
