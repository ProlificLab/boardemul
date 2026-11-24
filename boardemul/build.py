# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A Build: what a firmware project produces for one board (ADR 0182).

Read from a PlatformIO build folder (`.pio/build/ENV`). `firmware.bin` is
either a whole flash image, or -- what `pio run` makes -- the application
alone, in which case `bootloader.bin` and `partitions.bin` beside it are
placed with it as a flash is written: the bootloader at 0x0 (the
ESP32-S3's), the table at 0x8000, the application at its partition's
offset. A filesystem image beside them -- `littlefs.bin`, `spiffs.bin` or
`fatfs.bin` -- goes at the offset of the first filesystem data partition
in `partitions.bin`, read as ESP-IDF writes that table: 32-byte entries
starting 0xAA 0x50, then type, subtype, offset and size.
"""

from __future__ import annotations

import hashlib
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

APPLICATION = "firmware.bin"
BOOTLOADER = "bootloader.bin"
FILESYSTEMS = ("littlefs.bin", "spiffs.bin", "fatfs.bin")
PARTITIONS = "partitions.bin"
BOOTLOADER_OFFSET = 0x0         # the ESP32-S3's; the ESP32's is 0x1000
TABLE_OFFSET = 0x8000
IMAGE_MAGIC = 0xE9
TYPE_APP = 0x00

ENTRY = struct.Struct("<2sBBII16sI")
ENTRY_MAGIC = b"\xaa\x50"
TYPE_DATA = 0x01
# fat, spiffs, littlefs (esp_partition.h); Arduino's LittleFS images often
# go in a partition still typed spiffs, so all three count.
FILESYSTEM_SUBTYPES = (0x81, 0x82, 0x83)


class BuildError(ValueError):
    """A folder that is not a build this can place on a board."""


@dataclass(frozen=True)
class Build:
    application: Path
    filesystem: Path | None = None
    filesystem_offset: int | None = None

    @classmethod
    def from_dir(cls, folder: str | Path) -> "Build":
        folder = Path(folder)
        application = folder / APPLICATION
        if not application.is_file():
            raise BuildError(f"no {APPLICATION} in {folder}")
        if not is_whole_image(application.read_bytes()) and all(
                (folder / name).is_file() for name in (BOOTLOADER, PARTITIONS)):
            # What `pio run` makes; an image alone is the emulator's to judge.
            application = merged(folder)
        filesystem = next((folder / n for n in FILESYSTEMS if (folder / n).is_file()), None)
        if filesystem is None:
            return cls(application)
        table = folder / PARTITIONS
        if not table.is_file():
            raise BuildError(f"{filesystem.name} is in {folder} but no {PARTITIONS} "
                             "says where it goes")
        offset = filesystem_offset(table.read_bytes())
        if offset is None:
            raise BuildError(f"{PARTITIONS} in {folder} has no filesystem partition "
                             f"for {filesystem.name}")
        return cls(application, filesystem, offset)

    @property
    def overlays(self) -> tuple[str, ...]:
        """The filesystem as the emulator's overlay spec, if there is one."""
        if self.filesystem is None:
            return ()
        return (f"0x{self.filesystem_offset:x}={self.filesystem}",)


def is_whole_image(image: bytes) -> bool:
    """Whether an image holds a partition table where the bootloader reads
    it, which an application alone does not."""
    return image[TABLE_OFFSET:TABLE_OFFSET + 2] == ENTRY_MAGIC


def app_offset(table: bytes) -> int | None:
    """The offset of the first application partition, where a flash puts
    the application and the bootloader starts it."""
    for at in range(0, len(table) - ENTRY.size + 1, ENTRY.size):
        magic, kind, _subtype, offset, _size, _label, _flags = ENTRY.unpack_from(table, at)
        if magic != ENTRY_MAGIC:
            break
        if kind == TYPE_APP:
            return offset
    return None


def merged(folder: Path) -> Path:
    """The whole flash image of a folder whose firmware.bin is the
    application alone, made once per content in the system's temporary
    folder, so the build folder is not written to."""
    pieces = {name: folder / name for name in (BOOTLOADER, PARTITIONS, APPLICATION)}
    missing = [name for name, path in pieces.items() if not path.is_file()]
    if missing:
        raise BuildError(f"{APPLICATION} in {folder} is an application alone, and "
                         f"{' and '.join(missing)} beside it {'is' if len(missing) == 1 else 'are'} "
                         "missing to make a whole flash")
    boot, table, app = (pieces[n].read_bytes() for n in (BOOTLOADER, PARTITIONS, APPLICATION))
    if not boot or boot[0] != IMAGE_MAGIC or not app or app[0] != IMAGE_MAGIC:
        raise BuildError(f"{BOOTLOADER} and {APPLICATION} in {folder} are not ESP images")
    offset = app_offset(table)
    if offset is None:
        raise BuildError(f"{PARTITIONS} in {folder} has no application partition")
    if len(boot) > TABLE_OFFSET - BOOTLOADER_OFFSET or len(table) > offset - TABLE_OFFSET:
        raise BuildError(f"the pieces in {folder} overlap where a flash puts them")
    image = bytearray(b"\xff" * (offset + len(app)))
    image[BOOTLOADER_OFFSET:BOOTLOADER_OFFSET + len(boot)] = boot
    image[TABLE_OFFSET:TABLE_OFFSET + len(table)] = table
    image[offset:offset + len(app)] = app
    digest = hashlib.sha256(image).hexdigest()[:16]
    out = Path(tempfile.gettempdir()) / "boardemul-builds" / f"{folder.name}-{digest}.bin"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_suffix(".part")
        part.write_bytes(image)
        part.replace(out)
    return out


def filesystem_offset(table: bytes) -> int | None:
    """The offset of the first filesystem data partition in a partition
    table, or None."""
    for at in range(0, len(table) - ENTRY.size + 1, ENTRY.size):
        magic, kind, subtype, offset, _size, _label, _flags = ENTRY.unpack_from(table, at)
        if magic != ENTRY_MAGIC:
            break
        if kind == TYPE_DATA and subtype in FILESYSTEM_SUBTYPES:
            return offset
    return None
