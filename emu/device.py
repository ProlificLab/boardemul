#!/usr/bin/env python3
"""A device folder: identity and storage that outlive a run (ADR 0133).

A device is a directory with nothing else in it:

    <name>/
        device.json     name, board, firmware path and hash, MAC, PSRAM
        flash.bin       the chip's flash, assembled once by mkflash
        efuse.bin       the eFuses
        sdcard.img      the card, when the board has one

This module owns the format. `tools/device.py` is the CLI over it; the
emulator takes `--device <folder>` and touches nothing here but the reads and
the card write-back.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from emu import sdimage

FORMAT = 1
MANIFEST = "device.json"
FLASH = "flash.bin"
EFUSE = "efuse.bin"
SDCARD = "sdcard.img"

# Boards that have a card, with the size their board module builds; both
# today build the same one, and the folder says so rather than assuming.
SD_BOARDS: dict[str, int] = {"tdeck-max": 32, "tdeck": 32}


class DeviceError(Exception):
    """Raised for anything a folder, its manifest or its firmware gets wrong."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Device:
    """One device folder, as its manifest describes it."""

    root: Path
    name: str
    board: str
    firmware: str                 # the path as it was given at creation
    firmware_sha256: str
    mac: str
    psram_mb: int
    psram_octal: bool
    created: str
    overlays: tuple[str, ...] = ()

    @property
    def flash(self) -> Path:
        return self.root / FLASH

    @property
    def efuse(self) -> Path:
        return self.root / EFUSE

    @property
    def sdcard(self) -> Path:
        return self.root / SDCARD

    @property
    def has_sdcard(self) -> bool:
        return self.sdcard.exists()


def folder(devices: Path, name: str) -> Path:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise DeviceError(f"{name!r} is not a device name")
    return devices / name


def create(devices: Path, name: str, firmware: Path, board: str = "tdeck-max",
           mac: str = "02:54:44:4D:00:01", psram_mb: int = 8,
           psram_octal: bool = False, overlays: tuple[str, ...] = (),
           build=None) -> Device:
    """Make the folder and assemble its storage once.

    `build` is the assembler, injected so tests can watch for its use: it
    takes (firmware, outdir, mac, overlays) and returns (flash, efuse). The
    production one is `emu.run.build_images`, which is `mkflash` under
    another name.
    """
    if build is None:
        from emu.run import build_images as build
    root = folder(devices, name)
    if root.exists():
        raise DeviceError(f"{root} already exists; remove it first")
    if not firmware.is_file():
        raise DeviceError(f"no firmware at {firmware}")
    root.mkdir(parents=True)
    try:
        flash, efuse = build(firmware, root, mac, list(overlays))
        if board in SD_BOARDS:
            (root / SDCARD).write_bytes(bytes(sdimage.build(size_mb=SD_BOARDS[board])))
        device = Device(
            root=root, name=name, board=board,
            firmware=str(firmware), firmware_sha256=_sha256(firmware),
            mac=mac, psram_mb=psram_mb, psram_octal=psram_octal,
            created=datetime.datetime.now().isoformat(timespec="seconds"),
            overlays=tuple(overlays),
        )
        save(device)
        return device
    except Exception:
        _force_clear(root)
        raise


def _force_clear(root: Path) -> None:
    """A failed create leaves nothing behind: a half device is a lie."""
    for path in (root / SDCARD, root / FLASH, root / EFUSE, root / MANIFEST):
        path.unlink(missing_ok=True)
    if root.is_dir():
        root.rmdir()


def load(root: Path) -> Device:
    manifest = root / MANIFEST
    if not manifest.is_file():
        raise DeviceError(f"{root} is not a device folder")
    fields = json.loads(manifest.read_text(encoding="utf-8"))
    for missing in ("name", "board", "firmware", "mac"):
        if missing not in fields:
            raise DeviceError(f"{manifest} has no {missing!r}")
    device = Device(
        root=root, name=fields["name"], board=fields["board"],
        firmware=fields["firmware"],
        firmware_sha256=fields.get("firmware_sha256", ""),
        mac=fields["mac"], psram_mb=fields.get("psram_mb", 8),
        psram_octal=fields.get("psram_octal", False),
        created=fields.get("created", ""), overlays=tuple(fields.get("overlays", ())),
    )
    if not device.flash.is_file() or not device.efuse.is_file():
        raise DeviceError(f"{root} has no assembled storage; run device.py flash")
    return device


def save(device: Device) -> None:
    device.root.mkdir(parents=True, exist_ok=True)
    fields = {
        "format": FORMAT,
        "name": device.name,
        "board": device.board,
        "firmware": device.firmware,
        "firmware_sha256": device.firmware_sha256,
        "mac": device.mac,
        "psram_mb": device.psram_mb,
        "psram_octal": device.psram_octal,
        "created": device.created,
        "overlays": list(device.overlays),
    }
    (device.root / MANIFEST).write_text(
        json.dumps(fields, indent=2) + "\n", encoding="utf-8")


def reflash(device: Device, firmware: Path, overlays: tuple[str, ...] = (),
            build=None) -> Device:
    """Re-assemble an existing device's storage around a new image. The card
    and the folder stay; the manifest records the new firmware."""
    if build is None:
        from emu.run import build_images as build
    if not firmware.is_file():
        raise DeviceError(f"no firmware at {firmware}")
    flash, efuse = build(firmware, device.root, device.mac, list(overlays))
    updated = Device(**{**device.__dict__, "firmware": str(firmware),
                        "firmware_sha256": _sha256(firmware),
                        "overlays": tuple(overlays)})
    save(updated)
    return updated


def remove(device: Device) -> None:
    for path in (device.sdcard, device.flash, device.efuse, device.root / MANIFEST):
        path.unlink(missing_ok=True)
    if device.root.is_dir():
        device.root.rmdir()


def all_devices(devices: Path) -> list[Device]:
    if not devices.is_dir():
        return []
    return sorted((load(p) for p in devices.iterdir()
                   if (p / MANIFEST).is_file()), key=lambda d: d.name)


def load_sdcard(device: Device, expected_bytes: int) -> bytearray:
    """The card's contents, from the folder when it is there, empty otherwise.

    `expected_bytes` is the length of the card the board built: a file of any
    other size is refused rather than resized, because truncating a card the
    guest has written is not a decision to make silently.
    """
    path = device.sdcard
    if not path.is_file():
        return bytearray(expected_bytes)
    image = path.read_bytes()
    if len(image) != expected_bytes:
        raise DeviceError(
            f"{path} holds {len(image)} bytes but the board's card is "
            f"{expected_bytes}: the device was created for another board, or "
            "the card was replaced. Recreate the device to change its card.")
    return bytearray(image)


def save_sdcard(device: Device, image: bytearray) -> None:
    device.sdcard.write_bytes(bytes(image))
