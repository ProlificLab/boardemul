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

The second board runs LilyGO's own `UnitTest` firmware, unmodified, all the way
into its LVGL interface -- and the interface answers keys and taps. In one
110-second run: 740 frames at a steady ten a second, every injected keystroke
delivered and echoed by the firmware itself (`Key pressed : 0x69`), 66 touch
reports read. The status bar shows a modelled battery voltage; the WiFi scan
finds no networks, correctly; the panel says "Radio is offline", which is the
LoRa radio honestly reporting absent.

Getting there is five records long, and each one is a wall the firmware stopped
at and how it was read rather than guessed: the FPU enabled out of reset
(ADR 0013), both I2S directions with one patch carried to QEMU's GDMA
(ADR 0016, 0017), four radio bring-up waits answered from disassembled loops
(ADR 0019), and the SAR ADC that `analogRead()` spins on plus the touch
interrupt's real polarity (ADR 0020).

```
python3 emu/run.py firmware/T-Deck_UnitTest_250620.bin --board tdeck --bus --timeout 3600
```

```
python emu/app.py --board tdeck --scale 2
```

Meshtastic also boots on this board (`--psram-octal`): with its filesystem
image in place (`--overlay 0xc90000=firmware/littlefs-t-deck-tft-2.7.10.94d4bdf.bin`,
ADR 0021) a fresh first boot mounts storage, reads its preferences and, some
30 seconds of guest time in, reaches the device-ui's setup dialog (ADR 0026).
The dialog can be filled from the keyboard through the input file: byte 0x09
moves LVGL's focus from the region to the names and on to OK, letters land in
the focused field, and 0x0D on OK saves the names and region, after which the
firmware reboots itself and comes back to its home screen with the LoRa
frequency set. Two things to know when driving it. The interface blanks its
panel 30 seconds after LVGL starts unless some input device has reported a
press, and on this guest that is a few seconds after the dialog appears, so the
first key has to go in promptly; once blanked, the screen is locked and only
its own blank-screen button releases it. And touch: Meshtastic's LovyanGFX
driver reads the touch controller only while its interrupt line is low, and
never writes the controller's trigger mode, whereas the vendor firmware writes
mode 0 and treats that same line as pressed when high. The controller model
resolves the conflict by following the mode each firmware wrote, resting the
line low until one is (ADR 0029). The controller also reports in its own
raster, portrait like the panel behind it, with the board stating the mounting
(ADR 0030). With both in place the dialog can be filled by touch alone: tapping
the little keyboard icon beside a field is what binds the keypad to it, typed
characters land in that field, and OK commits and reboots. Tapping the field
itself does not bind, which is the interface's own design.
Run it with `--icount` (ADR 0027):
the Bluetooth link layer, which the device-ui switches on when its boot logo
is held, needs the instruction-counted clock to keep its events on time; its
core's clock, timers and interrupt FIFO are a C device read off the ROM
(ADR 0025). Nothing transmits.

Because neither firmware exercises much of the panel, the window arithmetic,
orientation and segment handling are checked against synthetic wire traffic
(`tests/test_lcd.py`), and what a frame even means on a display with no refresh
command is ADR 0014.

Two boards can be put on the same air with `--air DIR`, and `--at X,Y`,
`--environment` and `--antenna` say where they stand and what is between them
(ADRs 0036-0039). A node only transmits once it has a region, which the setup
dialog gives it: `tools/make_configured_fs.py` does that by driving the dialog
and keeps the filesystem it produces in `build/`, so the two-node mesh in
`tests/test_mesh.py` rebuilds from the vendor image and nothing else.

Bluetooth advertises on an air of its own with `--ble-air DIR`, built the same
way (ADR 0045). `tools/make_configured_fs.py --bluetooth` captures a filesystem
with it switched on, and what goes out is what the controller staged: the
payload is not copied anywhere, so the core resolves the record's offsets
through the controller's own mappings and sends what they point at
(ADRs 0044-0045). It advertises, and it can be connected to: a central on that air sends a
connection request, the firmware's own link layer validates it and starts
scheduling connection events (ADRs 0046-0047). Meshtastic never scans, so
two boards cannot find each other over Bluetooth; a central connecting in is
the peer it was built for. The link comes up and carries nothing yet.

## Tests

```
python3 -m pytest tests/            # 170 device, image and board tests, ~5s
python3 -m pytest tests/ -m slow    # boots both boards under QEMU, ~10 min
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
is six build lines across two `meson.build` files, plus three patches to their
own logic: a GDMA transmit path that never reported which descriptor finished,
which crashed the firmware's interrupt handler (ADR 0017); an SPI1 dummy-cycle
conversion that shifted every runtime flash read by two bytes, which no
filesystem survived (ADR 0022); and two Bluetooth clock registers the SYSTEM
model forgot, which the controller checks before it will start (ADR 0023).
Each is applied from a declared patch file, each is reported upstream, and the
build stops if any ever fails to apply. See ADR 0001, ADR 0011, ADR 0017,
ADR 0022 and ADR 0023.

## Dependencies

One large external component: the Espressif QEMU fork, pinned by tag and used as
the CPU and SoC engine only (ADR 0001). Everything else is the Python 3.12
standard library. `emu/app.py` uses tkinter, which is also standard library.

## Reading order

Start with `docs/BOOT.md`, then the ADRs in order. ADR 0008 is the one with the
most transferable lesson.
