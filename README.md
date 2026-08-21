# BoardEmul

An emulator for one specific device: the LilyGO T-Deck Max (ESP32-S3), complete
enough that the real, unmodified, vendor-compiled firmware boots and runs.

This is not a network simulator and not a general emulation framework. It
targets one board.

## Structure

    docs/BOOT.md      the ESP32-S3 boot chain, and what QEMU provides for each stage
    docs/adr/         numbered architecture decision records
    tools/            build-time tooling (QEMU fetch/build, flash image assembly)
    emu/              the emulator: QEMU launcher and post-run analysis
    firmware/         vendor binaries (not committed; see MANIFEST.toml)
    reports/          generated analysis output

## Current milestone

**M0 — boot the vendor firmware under QEMU and report what hardware it needs
that we do not emulate.** A truthful negative result is the expected outcome.

Out of scope until M0 is done: peripheral models, the bus bridge, any GUI,
radio, mesh protocol, board description format, or multi-device support.

## Dependencies

One large external component: the Espressif QEMU fork, used as the CPU and SoC
engine only (ADR 0001). Everything else is the Python 3.12 standard library.
