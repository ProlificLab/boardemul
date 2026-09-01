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

The second board is live and interactive: ST7789 display, GT911 touch and the
keyboard are all modelled, and the drawn device in `emu/app.py` knows how to be
a T-Deck as well as a T-Deck Max.

LilyGO's own `UnitTest` firmware boots, initialises the display, reports
`Init GT911 Sensor success!` and `Keyboard registered!!` against our models, and
draws its splash screen. It then blocks in its audio test, waiting on a GDMA
transfer that nothing completes -- so for something that keeps responding, the
Meshtastic build is the one to drive:

```
python3 emu/run.py firmware/firmware-t-deck-tft-2.7.10.94d4bdf.bin     --board tdeck --bus --psram-octal --timeout 600
```

```
python emu/app.py --board tdeck --scale 2
```

Typing and tapping in that window reach the firmware: it polls the keyboard
hundreds of times a run, reads touch reports, and redraws in response. Note the
`--psram-octal`: which line mode an image needs turns out to depend on the
firmware, not the board (ADR 0007, amended).

Because the firmware exercises so little of the panel, the window arithmetic,
orientation and segment handling are checked against synthetic wire traffic
(`tests/test_lcd.py`), and what a frame even means on a display with no refresh
command is ADR 0014.

## Tests

```
python3 -m pytest tests/            # 158 device, image and board tests, ~7s
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

Our C is six files we own plus a header, copied into the QEMU tree by
`tools/build_qemu.sh` rather than committed to it. Their tree's own modification
is six build lines across two `meson.build` files, plus exactly one patch to
their own logic -- a GDMA transmit path that never reported which descriptor
finished, which crashed the firmware's interrupt handler. It is applied from a
declared patch file, the build stops if it ever fails to apply, and it has been
sent upstream. See ADR 0001, ADR 0011 and ADR 0017.

## Dependencies

One large external component: the Espressif QEMU fork, pinned by tag and used as
the CPU and SoC engine only (ADR 0001). Everything else is the Python 3.12
standard library. `emu/app.py` uses tkinter, which is also standard library.

## Reading order

Start with `docs/BOOT.md`, then the ADRs in order. ADR 0008 is the one with the
most transferable lesson.
