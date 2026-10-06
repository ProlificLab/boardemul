# ADR 0131 — The Max's three touch keys report as key events

**Status:** Accepted — 2026-10-06. Closes the item ADR 0130 left open: the
three keys printed on the glass were drawn but not modelled.

## Context

The T-Deck Max's face carries three touch keys on the glass below the
e-paper — a heart, a speech bubble and a paper plane (ADR 0130). They are not
keys of the TCA8418 keyboard and not touch points of the panel; they belong to
the touch controller, which reports them as key events.

The firmware that runs here carries the evidence, and LilyGO publishes the
same sources (`Xinyuan-LilyGO/T-Deck-MAX`):

- **`lib/HynTouch/src/hyn_cst3xx.c`** — the CST328 driver the factory firmware
  compiled in (its strings `hyn_cst3xx`, `cst3xx_updata_tpinfo failed` are in
  the vendor binary). Its report read is seven bytes from 0xD000, the same
  read the touch path already serves:

  - `buf[5] & 0x80` set is a **key** report; `buf[5] == 0x80` is a key alone,
    with no fingers. The existing one-finger report's `buf[5] = 0x01` is one
    finger with the key bit clear — which is what the model already sends.
  - `key_id = (buf[1] >> 4) - 1`: the key's number, one-based, in the high
    nibble of the second byte.
  - `key_state = buf[0]`, and **`0x83` exactly is a press**; any other value
    with bit 7 set is a release.
  - `0xD1F4` byte 3 is `fw_key_num`: how many touch keys the part has.

- **`lib/HynTouch/src/HynTouch.cpp`** — the Arduino wrapper keeps three keys
  (`key_id < 3`), fires a callback on press/release *transitions* only, and
  notices keys inside `hyn_touch_get_point()`: the same INT-driven poll that
  reads touches. So a key is delivered exactly like a touch — by a falling
  edge on the INT line — and repeated reports of one held key are harmless.

- **`examples/factory/factory.ino`** — the vendor firmware registers
  `factory_touch_key_event`, which pops the screen manager on any of the
  three keys pressed: they are the interface's back key. So the keys are not
  decoration; the factory UI answers them.

Two things the sources do not say:

- The release byte's exact value (only that it carries bit 7 and is not
  0x83). The model emits 0x81.
- Which icon is which key id (the firmware treats all three identically, so
  nothing here can tell them apart). The face numbers them left to right.

## Decision

1. **The CST328 model gains a key report** (`emu/devices/i2c_devices.py`).
   A `key` state of `(id, down)` produces the seven bytes the driver parses:
   `buf[0] = 0x83/0x81`, `buf[1] = (id+1) << 4`, `buf[5] = 0x80`, and the
   0xAB tail. A key report and a finger report cannot be set at once; the
   key, like the finger, clears when its release is read. Chip info byte 3
   reports three keys, as `fw_key_num`.

2. **The input protocol gains a `touchkey` event** —
   `{"type": "touchkey", "key": 0..2, "down": bool}` — written by the window
   and routed by the board to the CST328 and its INT line, one edge per
   report and repeats while held, exactly as a touch is delivered.

3. **The window's three drawn icons become pressable** (ADR 0130's face
   reports their boxes): clicking one presses that key, with a wash over the
   icon while it is held. They sit below the panel, so they were never
   touches; now they are keys instead of nothing.

4. **The slow test proves the round trip through the vendor firmware**: tap a
   launcher icon to open a sub-screen, press a touch key, and the launcher
   comes back — `scr_mgr_pop`, as the factory's own callback does it.

## Consequences

**Good**

- The window's Max behaves like the device: the three keys under the screen
  take the interface back, in the vendor firmware and in anything else that
  registers the callback.
- The key report is byte-for-byte what `hyn_cst3xx.c` parses, asserted by
  holding the model's bytes to the driver's own conditions rather than to a
  restatement of them.

**Costs, accepted**

- The release value 0x81 and the icon-to-id order are the two measured-blind
  spots above; each is one line to change if the hardware says otherwise.
- A key held down re-pulses INT at the touch report rate, which real hardware
  would not do; the wrapper's transition-only callback makes that invisible
  to the firmware.
