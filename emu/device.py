#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
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
import os
import re
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from emu import sdimage

FORMAT = 1
MANIFEST = "device.json"
FLASH = "flash.bin"
EFUSE = "efuse.bin"
SDCARD = "sdcard.img"
# The MAC a device is given when it is the first in its folder; the next
# ones count up from it (ADR 0155, decision 5).
DEFAULT_MAC = "02:54:44:4D:00:01"
# What a device made from the page or `device.py create` starts with: it
# restarts after a panic, as the board does (ADR 0145, decision 6).
OPERATOR_SETTINGS = ("--reboot",)

# Boards that have a card, with the size their board module builds; both
# today build the same one, and the folder says so rather than assuming.
SD_BOARDS: dict[str, int] = {"tdeck-max": 32, "tdeck": 32}
# A device made from a board description keeps it here (ADR 0184, decision 6).
BOARD_FILE = "board.toml"


class DeviceError(Exception):
    """Raised for anything a folder, its manifest or its firmware gets wrong."""


class DeviceBusy(DeviceError):
    """Raised when a run holds the folder and the request needs it stopped."""


# The run settings a device keeps and a page may pass, and whether each takes
# a value (ADRs 0139, 0145). Nothing here names a file, a directory or a
# program: an air is named, and where its directory is depends on where the
# run is, which the run handle decides.
RUN_FLAGS = {
    "--wifi-ap": True, "--wifi-wpa2": False, "--wifi-pass": True,
    "--wifi-uplink": False, "--gps": True, "--at": True, "--antenna": True,
    "--environment": True, "--obstruction": True, "--icount": False,
    "--host-clock": False,
    "--reboot": False, "--air": True, "--ble-air": True,
    # What the speaker plays, to WAV files in the run's own directory (ADR
    # 0146). --mic names a file, so it stays on the command line.
    "--audio": False,
    # A serial port on the host's loopback, for its own tools (ADR 0158).
    "--serial-tcp": True,
    # A port of the host's loopback reaching one of the device's over WiFi
    # (ADR 0164); may be given more than once.
    "--wifi-forward": True,
}
SERIAL_TCP = re.compile(r"(\d{4,5})(:(uart|usb|otg))?\Z")
WIFI_FORWARD = re.compile(r"(\d{4,5}):(\d{1,5})\Z")
# The settings that put a port on the host's loopback, and how to read it.
PUBLISHED = {"--serial-tcp": lambda v: v.partition(":")[0],
             "--wifi-forward": lambda v: v.partition(":")[0]}
# The named airs, and the band each lives under.
NAMED_AIRS = {"--air": "lora", "--ble-air": "ble"}
AIR_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
# Where runs in the build container keep their airs, and the network they
# meet on (ADR 0145).
AIR_VOLUME = AIR_NETWORK = "boardemul-air"
AIR_MOUNT = "/air"


def check_settings(args) -> tuple[str, ...]:
    """Run settings held to RUN_FLAGS, each air a plain name. Raises
    DeviceError saying what is wrong."""
    if not isinstance(args, (list, tuple)):
        raise DeviceError("settings must be a list")
    out: list[str] = []
    i = 0
    while i < len(args):
        flag = args[i]
        if not isinstance(flag, str) or flag not in RUN_FLAGS:
            raise DeviceError(f"{flag!r} is not a setting a device takes")
        out.append(flag)
        i += 1
        if RUN_FLAGS[flag]:
            value = args[i] if i < len(args) else None
            if not isinstance(value, str) or value.startswith("--"):
                raise DeviceError(f"{flag} needs a value")
            if flag in NAMED_AIRS and not AIR_NAME.match(value):
                raise DeviceError(f"{flag} takes an air's name (lower-case "
                                  f"letters, digits and dashes), not {value!r}")
            if flag == "--serial-tcp":
                found = SERIAL_TCP.match(value)
                if not found or not 1024 <= int(found.group(1)) <= 65535:
                    raise DeviceError("--serial-tcp takes a port from 1024 to "
                                      f"65535, then :uart, :usb or :otg, not {value!r}")
            if flag == "--wifi-forward":
                found = WIFI_FORWARD.match(value)
                if (not found or not 1024 <= int(found.group(1)) <= 65535
                        or not 1 <= int(found.group(2)) <= 65535):
                    raise DeviceError("--wifi-forward takes HOSTPORT:DEVICEPORT, "
                                      f"the host port from 1024, not {value!r}")
            out.append(value)
            i += 1
    return tuple(out)


def place_airs(args: tuple[str, ...], base: str) -> tuple[str, ...]:
    """The same settings with each named air given its directory under `base`
    (ADR 0145)."""
    out = list(args)
    for i, flag in enumerate(out[:-1]):
        if flag in NAMED_AIRS:
            out[i + 1] = f"{base}/{NAMED_AIRS[flag]}/{out[i + 1]}"
    return tuple(out)


def uses_air(args: tuple[str, ...]) -> bool:
    return any(flag in NAMED_AIRS for flag in args)


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
    psram_octal: bool             # forced octal; False takes the image's mode (ADR 0144)
    created: str
    overlays: tuple[str, ...] = ()
    # What every run of it is told about the world around it (ADR 0145).
    settings: tuple[str, ...] = ()
    cloned_from: str = ""         # the device it was cloned from (ADR 0155)

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

    @property
    def board_file(self) -> Path:
        return self.root / BOARD_FILE

    def description(self):
        """The board description the device was made from, or None for one of
        the hand-written boards (ADR 0184)."""
        if not self.board_file.is_file():
            return None
        from emu import described
        try:
            return described.load(self.board_file)
        except described.DescriptionError as exc:
            raise DeviceError(str(exc)) from None


def folder(devices: Path, name: str) -> Path:
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise DeviceError(f"{name!r} is not a device name")
    return devices / name


def create(devices: Path, name: str, firmware: Path, board: str = "tdeck-max",
           mac: str | None = None, psram_mb: int = 8,
           psram_octal: bool = False, overlays: tuple[str, ...] = (),
           build=None, description=None) -> Device:
    """Make the folder and assemble its storage once.

    `build` is the assembler, injected so tests can watch for its use: it
    takes (firmware, outdir, mac, overlays) and returns (flash, efuse). The
    production one is `emu.run.build_images`, which is `mkflash` under
    another name.

    Without a `mac` the device gets one no other device in the folder has
    (ADR 0155, decision 5).

    With a `description` (an `emu.described.Description`) the device is that
    board: the folder keeps its text, and `board` is its name (ADR 0184).
    """
    if build is None:
        from emu.run import build_images as build
    root = folder(devices, name)
    if root.exists():
        raise DeviceError(f"{root} already exists; remove it first")
    if not firmware.is_file():
        raise DeviceError(f"no firmware at {firmware}")
    taken = {_mac_int(d.mac) for d in all_devices(devices)} if devices.is_dir() else set()
    if mac is None:
        mac = (DEFAULT_MAC if _mac_int(DEFAULT_MAC) not in taken
               else next_free_mac(devices, DEFAULT_MAC))
    elif _mac_int(mac) in taken:
        raise DeviceError(f"another device already has the MAC {mac}")
    root.mkdir(parents=True)
    try:
        if not overlays:
            # A pinned firmware brings its filesystem, as its installer would
            # (ADR 0150); the device records it among its overlays.
            from tools.fetch_firmware import companion_overlays
            overlays = tuple(companion_overlays(firmware)[0])
        flash, efuse = build(firmware, root, mac, list(overlays))
        if description is not None:
            board = description.name
            (root / BOARD_FILE).write_text(description.source, encoding="utf-8")
        card_mb = _card_mb(board, description)
        if card_mb:
            (root / SDCARD).write_bytes(bytes(sdimage.build(size_mb=card_mb)))
        device = Device(
            root=root, name=name, board=board,
            firmware=firmware.as_posix(), firmware_sha256=_sha256(firmware),
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
        # Written with forward slashes; a folder made on Windows before that
        # wrote backslashes, which Linux reads as part of a name (ADR 0140).
        firmware=fields["firmware"].replace("\\", "/"),
        firmware_sha256=fields.get("firmware_sha256", ""),
        mac=fields["mac"], psram_mb=fields.get("psram_mb", 8),
        psram_octal=fields.get("psram_octal", False),
        created=fields.get("created", ""), overlays=tuple(fields.get("overlays", ())),
        settings=check_settings(fields.get("settings", [])),
        cloned_from=fields.get("cloned_from", ""),
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
        "settings": list(device.settings),
    }
    if device.cloned_from:
        fields["cloned_from"] = device.cloned_from
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
    updated = Device(**{**device.__dict__, "firmware": firmware.as_posix(),
                        "firmware_sha256": _sha256(firmware),
                        "overlays": tuple(overlays)})
    save(updated)
    return updated


def set_settings(device: Device, settings) -> Device:
    """Keep these run settings in the device's manifest (ADR 0145)."""
    updated = Device(**{**device.__dict__, "settings": check_settings(settings)})
    save(updated)
    return updated


def remove(device: Device) -> None:
    for path in (device.sdcard, device.flash, device.efuse, device.root / MANIFEST,
                 device.root / RUNNING):
        path.unlink(missing_ok=True)
    shutil.rmtree(device.root / SNAPSHOTS, ignore_errors=True)
    if device.root.is_dir():
        device.root.rmdir()


# -- a run holds the folder while it runs (ADR 0149) --------------------------

RUNNING = "running"


def hold(device: Device) -> None:
    """Say that a run holds this folder until it ends: its card is written back
    then, and QEMU writes its flash as the guest does, so until then the files
    are not a state the device was ever in."""
    marker = device.root / RUNNING
    marker.write_text(f"{os.getpid()} {datetime.datetime.now().isoformat(timespec='seconds')}"
                      f" {socket.gethostname()}\n", encoding="utf-8")
    import atexit
    atexit.register(lambda: marker.unlink(missing_ok=True))


def _gone(pid: int) -> bool:
    """Whether no process has this number, as far as this machine can tell.
    Only asked on POSIX: on Windows signal 0 is CTRL_C_EVENT, which would
    interrupt the process rather than ask about it."""
    if os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False                  # it exists; it is only not ours to signal
    return False


def held(device: Device) -> str | None:
    """Since when a run has held the folder, or None.

    A run that was killed never removes its marker, and the folder then
    refused snapshots and cards for good. A marker written on this machine by
    a process that no longer exists is one of those, and is removed. One from
    another machine or container, or one without its machine's name, cannot
    be checked from here and still holds."""
    marker = device.root / RUNNING
    try:
        words = marker.read_text(encoding="utf-8").split()
    except OSError:
        return None
    if (len(words) > 2 and words[2] == socket.gethostname()
            and words[0].isdigit() and _gone(int(words[0]))):
        marker.unlink(missing_ok=True)
        return None
    return words[1] if len(words) > 1 else "an unknown time"


def _require_stopped(device: Device) -> None:
    since = held(device)
    if since is not None:
        raise DeviceBusy(
            f"{device.name} is running (since {since}); stop it first. If no "
            f"run holds it, remove {device.root / RUNNING}")


# -- snapshots of a stopped device's storage (ADR 0149) -----------------------

SNAPSHOTS = "snapshots"
SNAPSHOT_LABEL = re.compile(r"[A-Za-z0-9._-]{1,40}\Z")


@dataclass(frozen=True)
class Snapshot:
    label: str
    taken: str
    bytes: int


def _storage_files(device: Device) -> list[Path]:
    return [p for p in (device.flash, device.efuse, device.sdcard,
                        device.root / MANIFEST) if p.is_file()]


def _describe(folder: Path) -> Snapshot:
    taken = datetime.datetime.fromtimestamp(folder.stat().st_mtime)
    return Snapshot(label=folder.name, taken=taken.isoformat(timespec="seconds"),
                    bytes=sum(p.stat().st_size for p in folder.iterdir()
                              if p.is_file()))


def snapshot(device: Device, label: str | None = None) -> Snapshot:
    """Copy a stopped device's storage, manifest included, into
    snapshots/<label>/."""
    _require_stopped(device)
    label = label or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if not SNAPSHOT_LABEL.match(label):
        raise DeviceError(f"{label!r} is not a snapshot label: letters, digits, "
                          "'.', '_' and '-', 1 to 40 of them")
    target = device.root / SNAPSHOTS / label
    if target.exists():
        raise DeviceError(f"{device.name} already has a snapshot {label!r}")
    target.mkdir(parents=True)
    try:
        for path in _storage_files(device):
            shutil.copy2(path, target / path.name)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    os.utime(target)
    return _describe(target)


def snapshots(device: Device) -> list[Snapshot]:
    folder = device.root / SNAPSHOTS
    if not folder.is_dir():
        return []
    found = [_describe(p) for p in folder.iterdir()
             if p.is_dir() and (p / FLASH).is_file()]
    return sorted(found, key=lambda s: s.taken)


def _snapshot_folder(device: Device, label: str) -> Path:
    if not SNAPSHOT_LABEL.match(label or ""):
        raise DeviceError(f"{label!r} is not a snapshot label")
    folder = device.root / SNAPSHOTS / label
    if not (folder / FLASH).is_file():
        raise DeviceError(f"{device.name} has no snapshot {label!r}")
    return folder


def restore(device: Device, label: str) -> Device:
    """Put a stopped device back as the snapshot has it: storage, card and
    manifest, so its settings come back with the storage they were made with."""
    _require_stopped(device)
    folder = _snapshot_folder(device, label)
    for name in (FLASH, EFUSE, SDCARD, MANIFEST):
        kept = folder / name
        if kept.is_file():
            shutil.copy2(kept, device.root / name)
        elif name == SDCARD:
            # It had no card then.
            (device.root / name).unlink(missing_ok=True)
    return load(device.root)


def forget(device: Device, label: str) -> None:
    shutil.rmtree(_snapshot_folder(device, label))


# -- clones (ADR 0155) ---------------------------------------------------------

def _mac_int(mac: str) -> int:
    return int(mac.replace("-", ":").replace(":", ""), 16)


def _mac_text(value: int) -> str:
    return ":".join(f"{(value >> shift) & 0xFF:02X}" for shift in range(40, -8, -8))


def next_free_mac(devices: Path, after: str) -> str:
    """The first MAC counting up from `after` that no device in the folder
    has, within the same first three octets."""
    taken = {_mac_int(d.mac) for d in all_devices(devices)}
    value = _mac_int(after)
    prefix = value & ~0xFFFFFF
    for _ in range(0xFFFFFF):
        value = prefix | ((value + 1) & 0xFFFFFF)
        if value not in taken:
            return _mac_text(value)
    raise DeviceError("no free MAC left under " + after[:8])


def clone(device: Device, name: str, mac: str | None = None) -> Device:
    """A stopped device copied into a new folder, with a MAC of its own: its
    flash, card, settings and overlays as they are now, not its snapshots."""
    from tools import mkflash
    _require_stopped(device)
    devices = device.root.parent
    target = folder(devices, name)
    if target.exists():
        raise DeviceError(f"{target} already exists; remove it first")
    mac = mac or next_free_mac(devices, device.mac)
    try:
        efuse = mkflash.build_efuse(mkflash.parse_mac(mac))
    except ValueError as exc:
        raise DeviceError(f"{mac!r} is not a MAC: {exc}") from None
    if _mac_int(mac) in {_mac_int(d.mac) for d in all_devices(devices)}:
        raise DeviceError(f"another device already has the MAC {mac}")
    target.mkdir(parents=True)
    try:
        shutil.copy2(device.flash, target / FLASH)
        if device.sdcard.is_file():
            shutil.copy2(device.sdcard, target / SDCARD)
        (target / EFUSE).write_bytes(efuse)
        made = Device(**{**device.__dict__, "root": target, "name": name, "mac": mac,
                         "cloned_from": device.name,
                         "created": datetime.datetime.now().isoformat(timespec="seconds")})
        save(made)
        return made
    except Exception:
        _force_clear(target)
        raise


# -- the card, in and out (ADR 0149) ------------------------------------------

def _card_mb(board: str, description=None) -> int:
    """The size in MiB of the card a board builds, 0 for none."""
    if description is not None:
        from emu.described import SD_CARD_MB
        card = next((p for p in description.spi if p.part == "sdcard"), None)
        return int(card.options.get("size_mb", SD_CARD_MB)) if card else 0
    return SD_BOARDS.get(board, 0)


def card_bytes(device: Device) -> int:
    """The size of the card the device's board builds."""
    size = _card_mb(device.board, device.description())
    if not size:
        raise DeviceError(f"a {device.board} has no card")
    return size * 1024 * 1024


def _before_card(device: Device) -> Snapshot:
    return snapshot(device, "before-card-"
                    + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))


def replace_card(device: Device, image: bytes) -> Snapshot:
    """Make `image` the device's card, after a snapshot of the one it had.
    Returns that snapshot."""
    _require_stopped(device)
    if len(image) != card_bytes(device):
        raise DeviceError(f"a card image for a {device.board} is "
                          f"{card_bytes(device)} bytes, not {len(image)}")
    taken = _before_card(device)
    device.sdcard.write_bytes(image)
    return taken


SHORT_NAME = re.compile(r"[A-Z0-9_~!#$%&'()@^`{}-]{1,8}(\.[A-Z0-9_~!#$%&'()@^`{}-]{1,3})?\Z")


def card_from_files(device: Device, files: list[tuple[str, bytes]]) -> Snapshot:
    """A fresh card holding these files in its root directory, under their
    8.3 names, after a snapshot of the card it had."""
    names = [name.upper() for name, _data in files]
    bad = [name for name in names if not SHORT_NAME.match(name)]
    if bad:
        raise DeviceError(f"not 8.3 names: {', '.join(bad)}")
    if len(set(names)) != len(names):
        raise DeviceError("two files have the same name on the card")
    if len(files) > sdimage.ROOT_ENTRIES - 1:
        raise DeviceError("more files than the card's root directory holds")
    room = card_bytes(device) - sdimage.PARTITION_LBA * sdimage.SECTOR - 2 * 1024 * 1024
    if sum(len(data) for _name, data in files) > room:
        raise DeviceError("the files are more than the card holds")
    image = sdimage.build(size_mb=card_bytes(device) // (1024 * 1024),
                          files=tuple(sdimage.File(name, data)
                                      for name, (_n, data) in zip(names, files)))
    return replace_card(device, bytes(image))


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


def save_sdcard(device: Device, image: bytearray,
                 loaded_sha256: str | None = None) -> bool:
    """Put the card back in the folder at the end of a run (ADR 0133).

    Written beside the card and renamed over it, so a run stopped while it
    writes leaves the old card whole: a stop that the container's own grace
    period cut short once left 8 MB of a 32 MB card, and the device could not
    start again. Skipped when the run left the card as it found it, given
    the digest of what was loaded. Returns whether it wrote."""
    data = bytes(image)
    if loaded_sha256 is not None and hashlib.sha256(data).hexdigest() == loaded_sha256:
        return False
    part = device.sdcard.with_name(device.sdcard.name + ".part")
    part.write_bytes(data)
    os.replace(part, device.sdcard)
    return True


# A window-started run stops when its user stops it, not when a ceiling does
# (ADR 0134); a day is "never" with a safety net under it.
WINDOW_RUN_TIMEOUT = 24 * 60 * 60


REPO_ROOT = Path(__file__).resolve().parent.parent

# The build container a run moves into where it cannot run natively, and the
# image it is made from (.devcontainer/Dockerfile) unless told otherwise.
DEFAULT_IMAGE = "boardemul-build:latest"


def _run_flags(device: str, outdir: str, tag: str) -> list[str]:
    return ["--device", device, "--bus", "--outdir", outdir, "--tag", tag,
            "--timeout", str(WINDOW_RUN_TIMEOUT), "--stall", str(WINDOW_RUN_TIMEOUT)]


def launch_command(root: Path, outdir: Path, extra: tuple[str, ...] = (),
                   python: str = sys.executable) -> list[str]:
    """The command that starts a device as a run, for a caller that owns the
    child: the emulator's own flags, nothing clever (ADR 0134)."""
    return ([python, str(REPO_ROOT / "emu" / "run.py")]
            + _run_flags(str(root), str(outdir), root.name)
            + list(place_airs(tuple(extra), local_air_base(root))))


def local_air_base(root: Path) -> str:
    """Where a run on this host keeps its named airs: beside the device
    folders (ADR 0145)."""
    return (Path(root).resolve().parent / ".air").as_posix()


def runs_natively() -> bool:
    """Whether a run can start on this host as it is (ADR 0140).

    The board models talk to QEMU over Unix sockets, and QEMU is built in
    the build container as a Linux program; a Python without AF_UNIX is a
    host where neither holds. BOARDEMUL_RUN_IN=native|container overrides."""
    forced = os.environ.get("BOARDEMUL_RUN_IN", "").lower()
    if forced in ("native", "container"):
        return forced == "native"
    return hasattr(socket, "AF_UNIX")


def container_name(root: Path, outdir: Path) -> str:
    """A name Docker accepts, unique to this device and run."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", f"boardemul-{root.name}-{outdir.name}")
    return name.strip("-.") or "boardemul-run"


def container_command(root: Path, outdir: Path, extra: tuple[str, ...] = (),
                      name: str | None = None) -> list[str]:
    """The same run inside the build container (ADR 0140): the repository,
    the device folder and the run's output each mounted where the command
    names them, so no host path has to be translated."""
    def mount(host: Path, inside: str) -> list[str]:
        return ["-v", f"{Path(host).resolve().as_posix()}:{inside}"]
    image = os.environ.get("BOARDEMUL_IMAGE") or DEFAULT_IMAGE
    # On an air, the airs' volume and the network the boards meet on, each
    # board reached at its container's address there (ADR 0145).
    air = (["-v", f"{AIR_VOLUME}:{AIR_MOUNT}", "--network", AIR_NETWORK,
            "-e", "BOARDEMUL_AIR_HOST=auto"] if uses_air(extra) else [])
    # A serial port on TCP (ADR 0158) and ports forwarded over WiFi (ADR 0164),
    # published on the host's loopback only.
    published = [PUBLISHED[flag](extra[i + 1])
                 for i, flag in enumerate(extra[:-1]) if flag in PUBLISHED]
    for port in published:
        air += ["-p", f"127.0.0.1:{port}:{port}"]
    if published:
        air += ["-e", "BOARDEMUL_BIND_ALL=1"]
    return (["docker", "run", "--rm", "--name", name or container_name(root, outdir),
             "--stop-signal", "SIGINT"]
            + mount(REPO_ROOT, "/work") + mount(root, "/device") + mount(outdir, "/out")
            + air + ["-w", "/work", image, "python3", "emu/run.py"]
            + _run_flags("/device", "/out", root.name)
            + list(place_airs(tuple(extra), AIR_MOUNT)))


def ensure_air_network() -> None:
    """The Docker network boards on an air meet on, made the first time it is
    needed (ADR 0145)."""
    import subprocess
    found = subprocess.run(["docker", "network", "inspect", AIR_NETWORK],
                           capture_output=True)
    if found.returncode != 0:
        subprocess.run(["docker", "network", "create", AIR_NETWORK],
                       capture_output=True, check=True)


class RunHandle:
    """One device started as a child `emu/run.py --device` process.

    Whoever starts it owns it: the frames it watches and the input it sends
    are the child run's, and stopping it is the child's own interrupt, so
    the run's shutdown path — the card back to the folder, the manifest —
    is what happens (ADRs 0133, 0134). The Tk window and the web server
    both hold one of these.
    """

    def __init__(self, device, logs: Path | None = None,
                 extra: tuple[str, ...] = ()):
        self.device = device
        self.extra = extra             # per-run wiring: --wifi-ap, --air…
        runs = (logs or Path(__file__).resolve().parent.parent / "logs") \
            / "devices" / device.name
        runs.mkdir(parents=True, exist_ok=True)
        index = 1
        while (runs / str(index)).exists():
            index += 1
        # Reserved here rather than at start, so two handles never aim one
        # run's output directory at the same number.
        self.outdir = runs / str(index)
        self.outdir.mkdir(parents=True)
        self.proc: "object | None" = None
        # Set when the run is in the build container, to stop it by name.
        self.container: str | None = None

    def command(self) -> list[str]:
        """The command that runs this device, on this host or in the build
        container, whichever this host does (ADR 0140): the device's own
        settings, then this run's (ADR 0145)."""
        import subprocess
        settings = tuple(self.device.settings) + tuple(self.extra)
        if runs_natively():
            return launch_command(self.device.root, self.outdir, settings)
        self.container = container_name(self.device.root, self.outdir)
        if uses_air(settings):
            try:
                ensure_air_network()
            except (OSError, subprocess.CalledProcessError) as exc:
                raise RuntimeError(f"could not make the Docker network "
                                   f"{AIR_NETWORK}: {exc}") from None
        return container_command(self.device.root, self.outdir, settings,
                                 self.container)

    def start(self) -> None:
        import subprocess
        cmd = self.command()
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        # The child's console lands in its own run directory: a run.py that
        # dies at startup must say why where the run can be read (ADR 0133).
        console = open(self.outdir / "console.log", "wb")
        try:
            # Plain text in console.log: Python 3.13 colours its tracebacks
            # for a terminal, and this file is read in a page.
            self.proc = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=console,
                                         stderr=subprocess.STDOUT,
                                         env={**os.environ, "PYTHON_COLORS": "0"},
                                         creationflags=flags)
        except FileNotFoundError:
            console.close()
            if self.container is None:
                raise
            raise RuntimeError(
                "this host starts runs in the build container (ADR 0140), and "
                "docker is not on PATH: start Docker Desktop") from None

    def console_tail(self, lines: int = 40) -> list[str]:
        """The last lines the run's own process printed: why it ended, when
        it ended early (ADR 0140)."""
        try:
            text = (self.outdir / "console.log").read_text(encoding="utf-8",
                                                          errors="replace")
        except OSError:
            return []
        # Any colour codes a child wrote anyway are a terminal's, not a page's.
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text).splitlines()[-lines:]

    @property
    def alive(self) -> bool:
        import subprocess
        return (isinstance(self.proc, subprocess.Popen)
                and self.proc.poll() is None)

    def stop(self) -> None:
        import signal
        import subprocess
        if not isinstance(self.proc, subprocess.Popen) or self.proc.poll() is not None:
            return
        if self.container is not None:
            # The same interrupt a native stop sends, delivered inside.
            subprocess.run(["docker", "stop", "--signal", "SIGINT", "--time", "30",
                            self.container], capture_output=True, timeout=60)
        elif sys.platform == "win32":
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.proc.send_signal(signal.SIGINT)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=15)
