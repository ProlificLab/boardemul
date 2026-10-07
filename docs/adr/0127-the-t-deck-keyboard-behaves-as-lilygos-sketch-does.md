# ADR 0127 — The T-Deck keyboard behaves as LilyGO's sketch does

**Status:** Accepted — 2026-10-03. Replaces the "alternate layer is not
mapped" part of the keyboard model's notes; extends ADR 0126.

## Context

The T-Deck's keyboard is a second microcontroller, an ESP32-C3, running
LilyGO's own firmware. The main processor sees it as one I2C device at 0x55.
The model in `emu/devices/tdeck_i2c.py` reports any letter pressed, always in
lower case. It treats ALT, SYM, both shifts and the microphone key as keys
that send nothing. Its notes say the layer behind SYM was left unmapped
because it had never been seen on the wire, under ADR 0006.

The keyboard's firmware is published: `examples/Keyboard_ESP32C3` in
Xinyuan-LilyGO/T-Deck (MIT, revision 2025-06-12). It is the behaviour itself,
not a similar keyboard. It says:

- **The matrix.** Five columns by seven rows, 35 keys. One table gives each
  key's character; a second gives its symbol-layer character. Both have NULL
  for keys with nothing, such as SYM, ALT, both shifts, Enter and backspace.
  The microphone key has only a symbol, `0`.
- **What a press sends.** A press is a key's change from up to down between
  scans. If either table has an entry for the key, the reply is the symbol
  when SYM is held and the character otherwise, with NULL taken as zero. Then
  32 is subtracted when either shift is held. After that, Enter replaces the
  reply with 0x0D and backspace with 0x08. Alt+B toggles the keyboard
  backlight and cancels the reply, and Alt+C replaces it with 0x0C. ALT
  changes nothing else; only SYM is a layer.
- **The reply.** One byte: the latest one, given to the next read and then
  cleared. A read with nothing pending gets zero.
- **Commands written to it.** 0x01 sets the backlight duty and then, with no
  `break`, also runs 0x02's code, which reads one more byte. 0x02 sets the
  duty Alt+B turns on with, taken only when it is above 30. 0x03 switches to
  raw mode, where a read returns five bytes, one per column, with bit *r* set
  for each key held in row *r*. 0x04 switches back. The backlight boots at
  duty 0, and Alt+B's duty starts at 127.

Some odd results follow from that arithmetic, and the device has them too:

- SYM with space or `$` (which have no symbol) replies zero, which is nothing.
- A shift subtracts from whatever the layer gave. A shift with the microphone
  key gives (char)(0 − 32), 0xE0, because `char` is unsigned on the C3's
  RISC-V. SYM, a shift and T give 0x08, a backspace.

Two drivers in this repository type text by sending key-down events with
characters as key names: `tests/test_meshtastic_touch.py` and
`tools/make_configured_fs.py`, both typing `emu1`. Neither sends a release.
`1` is not a key on the T-Deck: it is SYM held with W.

The window forwards the host's shifts as the T-Deck's shifts and drops any
character that is not a letter. A mouse can press only one key at a time, so
nothing on the drawn device needs two keys.

## Decision

1. **The model is the sketch's matrix.** Each of the 35 keys has its column,
   row, character and symbol from the sketch's tables. The model tracks
   which keys are held. On a press it works out the byte the sketch would,
   from the same tables, in the same order (character or symbol, then shift,
   then Enter, backspace, Alt+B, Alt+C), with the same 8-bit arithmetic.

2. **Each press that produces a byte is queued, not overwritten.** The real
   keyboard keeps only the latest byte. That loses a key only when two
   presses fall between the main processor's polls, which a person cannot do
   on the device. Here input arrives on the host's clock, and the guest can
   run several times slower than it, so a single byte would lose keys that
   nobody typing on the device would lose. A press that replies zero queues
   nothing, because zero is what an idle keyboard replies. In raw mode the
   sketch goes on setting its reply byte, and the next read in key mode gets
   the latest one. So while raw, the queue keeps only the latest byte.

3. **Raw mode reports the matrix as the sketch does:** five bytes, column by
   column, bit *r* for row *r*. A key pressed and released between two raw
   reads still shows as held in the next one, for the same reason as the
   queue.

4. **Commands 0x01 to 0x04 are modelled as written**, including 0x01 running
   on into 0x02. The model holds the backlight's duty and on/off state and
   exposes them. Showing the glow in the window is a separate decision.

5. **Key events name keys, with their press and their release.** A press of
   a key that is already held counts as a new press, because a finger cannot
   press a held key without lifting it. This keeps drivers that send only
   presses working. The trackball's four directions are still accepted and
   send nothing until the trackball is modelled.

6. **A `text` event types characters.** The board presses each character as
   the chord that types it on the device and releases it at once. Plain
   characters are their key, capitals are LSHIFT with the letter, and the
   printed second characters are SYM with their key. Enter and backspace are
   their keys. A character the keyboard cannot type, such as `%`, is refused
   and logged rather than approximated. The two drivers that typed `emu1` as
   key names send `text` events instead.

7. **The window types the way the device does.**
   - A character typed on the host is sent as the chord that types it on the
     T-Deck: modifiers pressed, then the key, then released in reverse when
     the host key comes up. The host's shift is therefore not forwarded
     alone, because it is already in the character.
   - The host's Ctrl is SYM and its Alt is ALT, held for as long as the host
     holds them. Ctrl+Q types `#` and Alt+B toggles the backlight, as on the
     device.
   - Host auto-repeat is ignored, because the keyboard has none.
   - On the drawn device, alt, sym and both shifts latch when clicked. A
     latched key stays lit and held until the next key is clicked, which is
     pressed with it held; then it is released. Clicking a latched key again
     releases it. This is how a mouse holds two keys at once.

## Not decided here

The board pulsed GPIO 46 low on every key press, for a firmware that waits
on a keyboard interrupt, though the sketch drives no interrupt line. This
was resolved by ADR 0132: no firmware the emulator runs waits on the line,
and the pulse is gone.

## Consequences

**Good**

- Every character printed on a cap can be typed, with a click or from the
  host keyboard, and gives the byte the device gives.
- Alt+B and the brightness commands have state that the window can show.
- A firmware that uses raw mode reads the matrix it expects.

**Costs, accepted**

- The queue is a deliberate difference from the device's single byte, and
  is stated as one.
- The sketch's scan time is not modelled: about 35 ms per pass, with a 1 ms
  wait per key. Presses register as soon as they arrive, and a tap shorter
  than a scan, which the device would miss, is seen.
- Host characters the T-Deck cannot type are dropped, as on the device.
