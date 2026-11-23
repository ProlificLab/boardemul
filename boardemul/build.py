# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A Build: what a firmware project produces for one board (ADR 0182).

Read from a PlatformIO build folder (`.pio/build/ENV`): `firmware.bin` is
the application, and a filesystem image beside it -- `littlefs.bin`,
`spiffs.bin` or `fatfs.bin` -- goes at the offset of the first filesystem
data partition in `partitions.bin`, read as ESP-IDF writes that table:
32-byte entries starting 0xAA 0x50, then type, subtype, offset and size.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

APPLICATION = "firmware.bin"
FILESYSTEMS = ("littlefs.bin", "spiffs.bin", "fatfs.bin")
PARTITIONS = "partitions.bin"

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
