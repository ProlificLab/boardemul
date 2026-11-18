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

The front door is a browser:

```
python -m emu.web                # serves http://127.0.0.1:8420 and opens it
```

The page lists your devices, starts and stops them, shows the panel live
(long-polled frames), takes touches on the glass and typing on the keyboard
(the same chord logic the drawn window uses), tails the serial console, and
can scrub back through the run's frames.

On Windows the page and the browser stay on Windows and each run moves into
the build container, because the board models need Unix sockets and QEMU is
built there as a Linux program (ADR 0140). Docker Desktop has to be running,
with the image built and QEMU built into the tree:

```
docker build -t boardemul-build .devcontainer
docker run --rm -v "%CD%":/work -w /work boardemul-build bash -lc tools/build_qemu.sh
```

`BOARDEMUL_IMAGE` names another image; `BOARDEMUL_RUN_IN=native` or
`container` overrides the choice. The server serves only its own page and a
run takes per-run settings from it, never a program or a path (ADR 0139).

```
# in the devcontainer
python3 emu/run.py firmware/T-Deck-MAX_V3.1_20260313.bin --bus --timeout 200
python3 tools/make_viewer.py            # single-file HTML of the captured frames

# or the Tk window: pick a device, start it -- and click it
python emu/app.py --scale 2
# or watch a run's frames directly, as scripts do
python emu/app.py --frames logs/frames --scale 2
```

### Devices

A device is a folder that keeps its identity and storage between runs
(ADR 0133): `tools/device.py create pocket --firmware F.bin` assembles its
flash, eFuses and card once under `devices/pocket/`; `device.py start pocket`
or the web UI boots it, and what the guest saves is kept. Configure
Meshtastic's dialog once and every later boot is a node that already knows
itself.

**Working:** the boot chain; flash and eFuse assembly; the MMIO bridge with
interrupt delivery; I2C0/I2C1 with the board's devices; GPIO levels; SPI2; the
e-paper panel; PSRAM.

Touch input works: tapping an icon navigates. Click the panel in the web UI
or the Tk window -- the keyboard drawn on the shell is the vendor's own
matrix, so a cap that exists sends a key the firmware has. Events are
appended as JSON lines to `input.jsonl`, which the emulator tails, so a
script can drive it just as well as a person. The Max's three touch keys
under the screen are modelled too, and the vendor firmware answers them by
going back a screen (ADR 0131).

**Not working yet:** the Max's keyboard is wired but untested against a
screen that consumes keys -- the launcher is touch-only. PSRAM only
enumerates when presented in quad line mode, which is not what the board
actually has — see ADR 0007, which is the first thing to re-test after any
QEMU tag bump.

### The T-Deck, second board

The second board runs LilyGO's own `UnitTest` firmware, unmodified, all the way
into its LVGL interface -- and the interface answers keys and taps. In one
110-second run: 740 frames at a steady ten a second, every injected keystroke
delivered and echoed by the firmware itself (`Key pressed : 0x69`), 66 touch
reports read. The status bar shows a modelled battery voltage; the panel says
"Radio is offline", which is the LoRa radio honestly reporting absent. Given
`--wifi-ap NAME` its WiFi scan finds that network, on every scan (ADR 0057);
without one it finds nothing, correctly.

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

The window draws the T-Deck from LilyGO's own board drawing, product
photographs and keyboard sketch (ADR 0126), and it is used the way the device
is:

- **Typing.** Click the caps, or type on the host keyboard. Each character is
  pressed as the chord the T-Deck needs: capitals with its shift, the printed
  second characters with sym. The host's Ctrl is sym and its Alt is alt, so
  Ctrl+Q types `#` and Alt+B toggles the keyboard's backlight, exactly as the
  keyboard's own firmware does (ADR 0127).
- **Modifiers by mouse.** Clicking alt, sym or a shift latches it for the next
  key.
- **The trackball.** The mouse wheel rolls it (Shift+wheel for sideways), the
  arrow keys roll it a notch, and pressing the mouse on the ball clicks it for
  as long as you hold it (ADR 0128).
- **The keyboard backlight.** Light leaks round the caps while it is on
  (ADR 0129).

The Max's window is drawn the same way, from LilyGO's own case models
(ADR 0130): the rounded outline with its corner cut, the grey touch glass
with three touch keys printed under the screen, and the keyboard the TCA8418
matrix knows -- five keys in the bottom row, both outer ones up keys, and no
trackball, which the Max does not have. The three touch keys are modelled too
(ADR 0131): they report through the CST328 as key events, exactly as the
vendor's hyn_cst3xx driver parses them, and the vendor firmware answers them
by taking its interface back a screen.

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

A device, though, is now a thing that stays. `tools/device.py create NAME
--firmware F.bin` makes a folder in `devices/` holding the board's identity
and its storage -- flash, eFuses and card -- assembled once (ADR 0133).
`device.py start NAME -- --bus` boots it; what the guest saves is written
into the folder's own flash, so stopping and starting again finds it as it
was. Configure Meshtastic's dialog once and every later boot is a node that
already knows itself.

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
python3 -m pytest tests/            # 618 device, image and board tests, ~25s
python3 -m pytest tests/ -m slow    # boots both boards under QEMU, ~15 min
```

The slow tests skip cleanly without a built QEMU or the vendor firmware. See
ADR 0012.

## Structure

    docs/BOOT.md      the ESP32-S3 boot chain, and what QEMU provides for each stage
    docs/adr/         numbered architecture decision records
    qemu-patch/       the C we own, installed into the QEMU tree at build time
    tools/            QEMU fetch and build, flash image assembly, frame viewer,
                      device folders
    tests/            fast device tests, plus golden-frame tests behind -m slow
    emu/              the emulator: launcher, bus bridge host side, device models,
                      the drawn faces, the web front door
    firmware/         vendor binaries (not committed; see MANIFEST.toml)
    devices/          device folders, made by tools/device.py (ADR 0133)

## How it fits together

QEMU runs the CPU and the parts of the SoC it already models. The boundary
between C and Python is a bus transaction, not a register access (ADR 0011):
the SoC's controllers -- GPIO, SPI2, I2C0 and I2C1 -- are C, and what a
transaction then *means* is Python. The panel sees whole segments and an I2C
device sees whole exchanges, instead of either side counting FIFO writes. That
is what took the first board from 232,320 round trips per frame to about 44.

Our C is ten files we own plus a header, copied into the QEMU tree by
`tools/build_qemu.sh` rather than committed to it. Their tree's own modification
is a handful of build lines across two `meson.build` files, plus eleven carried
patches to their own logic, each reported upstream and listed in the build
script: a GDMA transmit path that never reported which descriptor finished
(ADR 0017); an SPI1 dummy-cycle conversion that shifted every runtime flash
read by two bytes (ADR 0022); two Bluetooth clock registers the controller
checks before it will start (ADR 0023); a cross-thread TLB flush that crashed
two-thread translation (ADR 0066); GDMA reaching PSRAM (ADR 0083); an
interrupt-matrix level mirror (ADR 0101); a UART chardev hook (ADR 0105); an
icount sleep that is not deterministic (ADR 0115); flash writes that land
synchronously (ADR 0115); UART RX overflow behaviour (ADR 0122); and core
reset-and-stall semantics (ADR 0123).
Each is applied from a declared patch file, and the build stops if any ever
fails to apply. See ADR 0001, ADR 0011, and the ADR beside each patch.

## Dependencies

One large external component: the Espressif QEMU fork, pinned by tag and used as
the CPU and SoC engine only (ADR 0001). Everything else is the Python 3.12
standard library: `emu/app.py` uses tkinter, the web UI is
`http.server` plus the browser you already have (ADR 0135).

## Reading order

Start with `docs/BOOT.md`, then the ADRs in order. ADR 0008 is the one with the
most transferable lesson; ADR 0011 is the boundary that made the rest possible;
ADRs 0126, 0130, 0131, 0133, 0134 and 0135 are the run of decisions that built
the two front doors and the devices between them.
