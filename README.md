# BoardEmul

Run and test your ESP32-S3 firmware without the board. BoardEmul emulates
the chip and the parts wired to it closely enough that real, unmodified
firmware boots and runs: your own build, or a vendor's. You drive it as a
person would -- the screen, the keys, the serial ports, the radio -- from a
page, a script or a CI job.

It comes with two boards, the LilyGO T-Deck Max and T-Deck, and is growing
toward boards its users describe themselves (ADR 0180). The words it uses --
Build, Device, Board, Part -- are in `GLOSSARY.md`.

## Getting started

With Python 3.11+ and Docker (ADR 0183):

```
pipx install boardemul
boardemul
```

The first time it pulls the runtime image, which carries QEMU, fetches the
vendor firmware `tools/firmware_manifest.toml` pins, and opens the page; your
devices, firmware and logs are kept in `~/.boardemul`. From a clone,
`python boardemul.py` does the same and builds the image from the clone's
own sources (ADR 0159). **`docs/GUIDE.md` is the operator's guide**:
devices, settings, snapshots and clones, a Meshtastic mesh, the scripting
API, and the limits.

## Testing your firmware

Point BoardEmul at your PlatformIO build folder: `firmware.bin`, and its
filesystem image placed where `partitions.bin` says (ADR 0182).

```python
from boardemul import Lab

with Lab() as lab:
    node = lab.device("node", build=".pio/build/t-deck", board="tdeck")
    node.start()
    node.serial("usb").expect(r"ready", timeout=120)
    node.tap(120, 80)
    node.screen.wait(lambda shot: shot.pixel(160, 120) != (0, 0, 0), timeout=60)
```

With pytest, the `boardemul_lab` fixture gives each test session a `Lab`.
From a shell or CI, `boardemul test` starts one device or several on one
LoRa air, runs your command, and answers with its exit status:

```
boardemul test --build .pio/build/t-deck --devices 2 -- python -m pytest tests/
```

In a GitHub workflow:

```yaml
- uses: prolificlab/boardemul@v0.1.0
  with:
    build: .pio/build/t-deck
    run: python -m pytest tests/emulated
```

Devices count their time in instructions, so a firmware keeps a board's
timing whatever the host is doing (ADR 0178).

## Scenarios

A test that is a sequence -- wait for this line, tap there, wait for that
picture -- can be a TOML file instead of Python (ADR 0185):

```toml
[[device]]
build = ".pio/build/t-deck"
board = "tdeck"

[[step]]
expect = "setup done"           # a line on the serial port
port = "usb"
timeout = 120

[[step]]
tap = [120, 80]

[[step]]
screen = "golden/menu.png"      # wait for a frame that matches
timeout = 30
```

`boardemul scenario checks/menu.toml` runs it, says each step as it
passes, stops at the first that does not, and answers 0 or 1. The steps
are `expect`, `send`, `tap`, `key`, `type`, `wait`, `screen`, `pixel` and
`save_screen`; several `[[device]]` entries share one LoRa air, and each
step names its `device`.

## Your own board

A board of your own, built from an ESP32-S3 and parts BoardEmul already
models, is a TOML file that says which part is on which pins (ADR 0184).
`emu/boards/tdeck.toml` is the T-Deck written that way; copy it and change
it:

```toml
name = "my-tracker"
chip = "esp32s3"

[[spi2.parts]]
part = "sx1262"
cs = 9
busy = 13
reset = 17
irq = 45

[uart1]
part = "l76k"
```

The catalogue is the two boards' parts. On `spi2`, three at most:
`st7789` or `epaper` (screens), `sx1262` (LoRa radio), `sdcard`. On `i2c0`
or `i2c1`: `gt911` and `cst328` (touch), `tdeck-keyboard` and `tca8418`
(keyboards), `es7210` and `es8311` (audio codecs), `xl9555`, `bhi260ap`,
`bq27220`, `drv2605`, `sy6970`. On `uart1` or `uart2`: `l76k` or
`ublox-m10` (GPS), `a7682e` (LTE modem). On plain pins: `tdeck-trackball`.
`emu/boards/tdeck-max.toml` is the other board written this way. Pass the file wherever a board goes:
`lab.device("node", build=..., board="my-tracker.toml")`,
`boardemul test --board my-tracker.toml`, or `--board` to `emu/run.py`. A
mistyped key or a pin used twice is refused with what is wrong. At the end
of a run, "not wired" lists what the firmware talked to that the
description leaves empty.

Developing the emulator itself uses the development image and builds QEMU
into `third_party/` (ADR 0147):

```
docker build -t boardemul-build .devcontainer
docker run --rm -v "%CD%":/work -w /work boardemul-build bash -lc tools/build_qemu.sh
python tools/fetch_firmware.py
```

`python tools/doctor.py` says whether anything is missing and what fixes it.

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
(the same chord logic the drawn window uses), tails either serial port and
takes a line typed into it (ADR 0156), plays what the speaker played, and
can scrub back through the run's frames.

On Windows the page and the browser stay on Windows and each run moves into
the build container, because the board models need Unix sockets and QEMU is
built there as a Linux program (ADR 0140): Docker Desktop has to be running,
with the runtime image `python boardemul.py` builds, or the development image
and QEMU built as above. `BOARDEMUL_IMAGE` names another image; `BOARDEMUL_RUN_IN=native` or
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

A device also keeps what every run of it is told about the world around it
(ADR 0145): an access point to join, a GPS position, and an air with its
place on it. Set them on the page's Settings sheet, or with
`device.py settings pocket -- --air mesh --at 50,0 --wifi-ap deck`. Devices
given the same air name hear each other's LoRa radios, on Windows too, where
each run is its own container: the runs then meet on a Docker network.

A stopped device can be snapshotted and put back, flash, card and settings
together, and its SD card is yours: download it as a FAT16 image, replace it
with another, or make a fresh one from files (ADR 0149). The page does all
three; so does `device.py snapshot|restore|card`. A stopped device can also
be cloned, flash, card and settings, with a MAC of its own, so one node set
up through its dialog becomes a mesh of them (ADR 0155).

Meshtastic's client API is reachable from this computer, as through a
cable and over WiFi. On the T-Deck its serial port is TinyUSB's, on the
chip's USB OTG controller, which is modelled with a host on its cable (ADR
0166): its log is in the page's console, and `tools/mesh_api.py` reads and
changes its settings over `--serial-tcp 5555:otg`. Set that way onto the
run's WPA2 access point and its classic screen, a node joins, takes an
address, and answers on `localhost:4403` through `--wifi-forward 4403:4403`
(ADR 0164).

**Working on the Max:** the boot chain, flash and eFuses; PSRAM; the e-paper
panel; touch, the three touch keys and the keyboard; the LoRa radio on chip
select 3 (ADR 0102); the SD card; I2C0/I2C1 with the board's parts, the fuel
gauge provisioned as its driver expects (ADR 0103) and the sensor hub booting
the firmware it is given (ADR 0108); the A7682E modem and the u-blox receiver
on their serial lines (ADRs 0105, 0117); WiFi, whose scan lists an access
point given `--wifi-ap` and whose own screen joins it (ADRs 0137, 0152,
0177); the chip's I2S controllers and
Bluetooth core, as on the T-Deck (ADR 0148); the USB serial port (ADR 0100).
The factory firmware's own Test page passes LoRa, Touch, SY6970, BQ27220,
SD Card, A7682E and ES8311.

Touch input works: tapping an icon navigates. Click the panel in the web UI
or the Tk window -- the keyboard drawn on the shell is the vendor's own
matrix, so a cap that exists sends a key the firmware has. Events are
appended as JSON lines to `input.jsonl`, which the emulator tails, so a
script can drive it just as well as a person. The Max's three touch keys
under the screen are modelled too, and the vendor firmware answers them by
going back a screen (ADR 0131).

The keyboard reaches the screens that read it: the launcher's icon grid is
touch-only by the vendor's design, and its sub-screens take keys
(`tests/test_max_keyboard.py`).

**Not working yet on the Max:** the ES8311 codec answers its identity and
nothing more, so its volume and routing are not modelled, though what the
firmware sends it over I2S is recorded with `--audio` (ADR 0148); the modem
answers only the few commands the factory test sends, and has no SIM
(ADR 0105). The factory interface's password field types its dialer's
digits, two per key, so a network it joins needs a passphrase of those
digits: `aaaa` types `22222222` (ADR 0152).

### The T-Deck, second board

The second board runs LilyGO's own `UnitTest` firmware, unmodified, all the way
into its LVGL interface -- and the interface answers keys and taps. In one
110-second run: 740 frames at a steady ten a second, every injected keystroke
delivered and echoed by the firmware itself (`Key pressed : 0x69`), 66 touch
reports read. The status bar shows a modelled battery voltage, and the LoRa
radio answers its bring-up ("Start Radio success!", ADR 0034). Given
`--wifi-ap NAME` its WiFi scan finds that network, on every scan (ADR 0057);
without one it finds nothing, correctly.

Getting there is five records long, and each one is a wall the firmware stopped
at and how it was read rather than guessed: the FPU enabled out of reset
(ADR 0013, now QEMU's own reset value, as on silicon: ADR 0143), both I2S
directions with one patch carried to QEMU's GDMA (ADR 0016, 0017), four radio bring-up waits answered from disassembled loops
(ADR 0019), and the SAR ADC that `analogRead()` spins on plus the touch
interrupt's real polarity (ADR 0020).

Its screen is lit as the firmware lights it (ADR 0151): the backlight's pin
is followed through the GPIO matrix, to an LED controller channel's duty or
to LilyGO's sixteen-step pulses, and the page and the window dim the panel
by it. When Meshtastic fades its screen out, the screen goes dark.

Its sound is real too (ADR 0146). With `--audio` each stream the speaker
plays is written to a WAV file in the run's `audio/` directory, which the
web page plays back; the UnitTest build plays six seconds of a sound of its
own. With `--mic FILE.wav` the microphone hears that file.
`tools/tone.py` writes a test tone and measures one.

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

Meshtastic also boots on this board, its PSRAM presented in octal because
that is the driver its image carries (ADR 0144). Its filesystem image comes
with it, as the vendor's installer writes it (ADR 0150): fetched, it is
overlaid at 0xc90000 unless `--overlay` says otherwise (ADR 0021), and
without it the interface stays black. A fresh first boot mounts storage,
reads its preferences and, some
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
follows the mode each firmware wrote (ADR 0029), and asserts the line while
it holds a report, release included, idling otherwise; a firmware that never
writes a mode gets the controller's default, low level (ADR 0031). The
controller also reports in its own
raster, portrait like the panel behind it, with the board stating the mounting
(ADR 0030). With both in place the dialog can be filled by touch alone: tapping
the little keyboard icon beside a field is what binds the keypad to it, typed
characters land in that field, and OK commits and reboots. Tapping the field
itself does not bind, which is the interface's own design.
It runs on the instruction-counted clock, the default (ADRs 0027, 0178):
the Bluetooth link layer, which the device-ui switches on when its boot logo
is held, needs the instruction-counted clock to keep its events on time; its
core's clock, timers and interrupt FIFO are a C device read off the ROM
(ADR 0025). What it then puts on the air is below.

Because neither firmware exercises much of the panel, the window arithmetic,
orientation and segment handling are checked against synthetic wire traffic
(`tests/test_lcd.py`), and what a frame even means on a display with no refresh
command is ADR 0014.

Two boards can be put on the same air with `--air DIR` (devices name one
instead, ADR 0145), and `--at X,Y`,
`--environment` and `--antenna` say where they stand and what is between them
(ADRs 0036-0039). A node only transmits once it has a region, which the setup
dialog gives it: `tools/make_configured_fs.py` does that by driving the dialog
and keeps the filesystem it produces in `build/`, so the two-node mesh in
`tests/test_mesh.py` rebuilds from the vendor image and nothing else.

A device folder (above) is the other way to keep a configured node: fill
the dialog once on a device, and every later boot of it already knows
itself.

Bluetooth advertises on an air of its own with `--ble-air DIR`, built the same
way (ADR 0045). `tools/make_configured_fs.py --bluetooth` captures a filesystem
with it switched on, and what goes out is what the controller staged: the
payload is not copied anywhere, so the core resolves the record's offsets
through the controller's own mappings and sends what they point at
(ADRs 0044-0045). It advertises, and it can be connected to: a central on that air sends a
connection request, the firmware's own link layer validates it and starts
scheduling connection events (ADRs 0046-0047). Meshtastic never scans, so
two boards cannot find each other over Bluetooth; a central connecting in is
the peer it was built for. The link carries data both ways: the core sends
what the controller stages in its transmit descriptors (ADR 0168), so link
control procedures, L2CAP, ATT and GATT all run, and `tools/ble_central.py`
is a central that reads and writes a firmware's GATT server. Meshtastic's
phone API answers over it: the central pairs with the node's PIN by LE
Secure Connections passkey entry, the controller derives the session key on
the core's own AES unit, every packet after that is AES-CCM on the air, and
the central gets the node's whole configuration stream, as an app does
(ADRs 0169, 0171). The Max carries the same Bluetooth core (ADR 0148).

## Tests

```
python3 -m pytest tests/            # some 700 device, image and board tests, ~2 min
python3 -m pytest tests/ -m slow    # some 120 that boot the boards under QEMU
bash tools/slow_suite.sh 3          # the same, three files at a time
```

The slow tests skip cleanly without a built QEMU or the vendor firmware. See
ADR 0012. Run several at a time, the ones marked `alone` still run one at a
time at the end: their firmware keeps deadlines on a clock that follows the
host's, and a busy host makes them miss (ADR 0167).

Where QEMU spends its time, in the build container and without perf:
`bash tools/profile.sh -- emu/run.py ARGS...` samples each of its threads
every millisecond and reports per thread, by function (ADR 0176).

## Structure

    docs/BOOT.md      the ESP32-S3 boot chain, and what QEMU provides for each stage
    docs/adr/         numbered architecture decision records
    qemu-patch/       the C we own, installed into the QEMU tree at build time
    tools/            QEMU build, firmware fetch, the doctor, flash image
                      assembly, frame viewer, device folders, the probes
    tests/            fast device tests, plus golden-frame tests behind -m slow
    emu/              the emulator: launcher, bus bridge host side, device models,
                      the drawn faces, the web front door
    firmware/         vendor binaries (not committed; see tools/firmware_manifest.toml)
    devices/          device folders, made by tools/device.py (ADR 0133)

## How it fits together

QEMU runs the CPU and the parts of the SoC it already models. The boundary
between C and Python is a bus transaction, not a register access (ADR 0011):
the SoC's controllers -- GPIO, SPI2, I2C0 and I2C1 -- are C, and what a
transaction then *means* is Python. The panel sees whole segments and an I2C
device sees whole exchanges, instead of either side counting FIFO writes. That
is what took the first board from 232,320 round trips per frame to about 44.

Our C is ten files we own plus a header, copied into the QEMU tree by
`tools/build_qemu.sh` rather than committed to it. Their tree's own
modification is a handful of build lines in one `meson.build`, plus four
carried patches to their own logic, listed in the build script: an icount
sleep that is not deterministic, in QEMU's core, and flash writes that land
synchronously (both ADR 0115, reported upstream), light sleep in the RTC
controller's model (ADR 0165), and register accesses that no longer end a
translation block without icount, in QEMU's core (ADR 0170); the last two
not yet offered. Nine more were carried
until Espressif took them into the tag we pin (ADR 0138): a GDMA transmit
path that never reported which descriptor finished (ADR 0017); an SPI1
dummy-cycle conversion that shifted every runtime flash read by two bytes
(ADR 0022); two Bluetooth clock registers the controller checks before it
will start (ADR 0023); a cross-thread TLB flush that crashed two-thread
translation (ADR 0066); GDMA reaching PSRAM (ADR 0083); an interrupt-matrix
level mirror (ADR 0101); a UART chardev hook (ADR 0105); UART RX overflow
behaviour (ADR 0122); and core reset-and-stall semantics (ADR 0123).
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
the two front doors and the devices between them, and ADRs 0145 and 0149
made a device something kept like a virtual machine: settings, snapshots,
its card.

## Licence

Copyright 2026 Prolific Lab. BoardEmul is under the Apache License 2.0
(`LICENSE`), except `qemu-patch/`, which is compiled into QEMU and is under
GPL-2.0-or-later (`qemu-patch/LICENSE`); `NOTICE` says which is which, and
vendor firmware is never part of it (ADR 0179). Contributions are welcome
with a sign-off: see `CONTRIBUTING.md`.
