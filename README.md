# BoardEmul

An emulator for one specific device: the LilyGO T-Deck Max (ESP32-S3), complete
enough that the real, unmodified, vendor-compiled firmware boots and runs.

This is not a network simulator and not a general emulation framework. It
targets one board.

## State

LilyGO's unmodified **factory firmware** boots under QEMU, brings up PSRAM and
its I2C devices, and draws its launcher to the emulated GDEQ031T10 e-paper
panel. Frames are written to `logs/frames/` as the run proceeds; `latest.png`
always holds the current screen.

```
# in the devcontainer
python3 emu/run.py firmware/T-Deck-MAX_V3.1_20260313.bin --bus --timeout 200
python3 tools/make_viewer.py            # single-file HTML of the captured frames

# on the host, to watch it live inside a drawn device
python emu/app.py --scale 2
```

**Working:** the boot chain; flash and eFuse assembly; the MMIO bridge with
interrupt delivery; I2C0/I2C1 with the board's devices; GPIO levels; SPI2; the
e-paper panel; PSRAM.

**Not working yet:** input is one-way, so the keyboard and touch panel cannot
reach the guest. The LoRa radio shares SPI2 and is ignored rather than modelled.
PSRAM only enumerates when presented in quad line mode, which is not what the
board actually has — see ADR 0007, which is the first thing to re-test after any
QEMU tag bump.

## Structure

    docs/BOOT.md      the ESP32-S3 boot chain, and what QEMU provides for each stage
    docs/adr/         numbered architecture decision records
    qemu-patch/       our single C file, installed into the QEMU tree at build time
    tools/            QEMU fetch and build, flash image assembly, frame viewer
    emu/              the emulator: launcher, bus bridge host side, device models
    firmware/         vendor binaries (not committed; see MANIFEST.toml)

## How it fits together

QEMU runs the CPU and the parts of the SoC it already models. A single C file
(`qemu-patch/esp_bus_bridge.c`) claims chosen MMIO windows and forwards every
access over a Unix socket. Everything with behaviour — the I2C controller, SPI2,
GPIO, the panel, the I2C devices — is Python on the other end of that socket.

The QEMU tree's total modification is **one appended line** in
`hw/misc/meson.build`; the C file is copied in by `tools/build_qemu.sh` rather
than committed to their tree. See ADR 0001 and ADR 0005.

## Dependencies

One large external component: the Espressif QEMU fork, pinned by tag and used as
the CPU and SoC engine only (ADR 0001). Everything else is the Python 3.12
standard library. `emu/app.py` uses tkinter, which is also standard library.

## Reading order

Start with `docs/BOOT.md`, then the ADRs in order. ADR 0008 is the one with the
most transferable lesson.
