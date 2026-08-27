# BoardEmul

An emulator for one specific device: the LilyGO T-Deck Max (ESP32-S3), complete
enough that the real, unmodified, vendor-compiled firmware boots and runs.

This is not a network simulator and not a general emulation framework. A
second board, the original T-Deck, is brought up alongside the first -- not to
become a framework, but because one board cannot tell you which parts of a
design were general and which were that board's accidents.

## State

LilyGO's unmodified **factory firmware** boots under QEMU, brings up PSRAM and
its I2C devices, and draws its launcher to the emulated GDEQ031T10 e-paper
panel. Frames are written to `logs/frames/` as the run proceeds; `latest.png`
always holds the current screen.

```
# in the devcontainer
python3 emu/run.py firmware/T-Deck-MAX_V3.1_20260313.bin --bus --timeout 200
python3 tools/make_viewer.py            # single-file HTML of the captured frames

# on the host, to watch it live inside a drawn device -- and click it
python emu/app.py --scale 2
```

**Working:** the boot chain; flash and eFuse assembly; the MMIO bridge with
interrupt delivery; I2C0/I2C1 with the board's devices; GPIO levels; SPI2; the
e-paper panel; PSRAM.

Touch input works: tapping an icon navigates. Click the panel in `emu/app.py`,
or type on it -- the keyboard drawn on the shell is the vendor's own matrix, so
a cap that exists sends a key the firmware has. Events are appended as JSON
lines to `logs/input.jsonl`, which the emulator tails, so a script can drive it
just as well as a person.

**Not working yet:** the keyboard is wired but untested against a screen that
consumes keys -- the launcher is touch-only. The LoRa radio shares SPI2 and is ignored rather than modelled.
PSRAM only enumerates when presented in quad line mode, which is not what the
board actually has — see ADR 0007, which is the first thing to re-test after any
QEMU tag bump.

### The T-Deck, second board

LilyGO's `UnitTest` firmware boots, completes startup, and runs on into the
app's own setup, where SPIFFS and the SD card both fail -- the image carries no
filesystem data and no card is modelled.

Getting there took one deliberate divergence: the FPU is left enabled out of
reset, because this firmware runs a float in a C++ global constructor, before
FreeRTOS exists to own the FPU state. It is opt-in per board and argued in
ADR 0013, which is also honest about what it could not confirm.

The ST7789 display is modelled and renders colour frames; GT911 touch and the
keyboard are not. The vendor firmware blocks on I2S before it reaches its
display test, so the pictures so far come from Meshtastic, which draws two
full-screen fills during boot. Its pixels *do* cross our SPI boundary -- 307,200
bytes of them, exactly two frames -- which is the question ADR 0011 left open.

Because that firmware exercises so little of the panel, the window arithmetic,
orientation and segment handling are checked against synthetic wire traffic
instead (`tests/test_lcd.py`), and what a frame even means on a display with no
refresh command is ADR 0014.

```
python3 emu/run.py firmware/T-Deck_UnitTest_251113.bin --board tdeck --bus
```

## Tests

```
python3 -m pytest tests/            # 124 device, image and board tests, ~6s
python3 -m pytest tests/ -m slow    # boots both boards under QEMU, ~6 min
```

The slow tests skip cleanly without a built QEMU or the vendor firmware. See
ADR 0012.

## Structure

    docs/BOOT.md      the ESP32-S3 boot chain, and what QEMU provides for each stage
    docs/adr/         numbered architecture decision records
    qemu-patch/       the C we own, installed into the QEMU tree at build time
    tools/            QEMU fetch and build, flash image assembly, frame viewer
    tests/            fast device tests, plus golden-frame tests behind -m slow
    emu/              the emulator: launcher, bus bridge host side, device models
    firmware/         vendor binaries (not committed; see MANIFEST.toml)

## How it fits together

QEMU runs the CPU and the parts of the SoC it already models. The boundary
between C and Python is a bus transaction, not a register access (ADR 0011):
the SoC's controllers -- GPIO, SPI2, I2C0 and I2C1 -- are C, and what a
transaction then *means* is Python. The panel sees whole segments and an I2C
device sees whole exchanges, instead of either side counting FIFO writes. That
is what took the first board from 232,320 round trips per frame to about 44.

Our C is five files we own plus a header, copied into the QEMU tree by
`tools/build_qemu.sh` rather than committed to it. The tree's own modification
is **five build lines** across two `meson.build` files, and nothing else. See
ADR 0001, ADR 0005 and ADR 0011.

## Dependencies

One large external component: the Espressif QEMU fork, pinned by tag and used as
the CPU and SoC engine only (ADR 0001). Everything else is the Python 3.12
standard library. `emu/app.py` uses tkinter, which is also standard library.

## Reading order

Start with `docs/BOOT.md`, then the ADRs in order. ADR 0008 is the one with the
most transferable lesson.
