#!/usr/bin/env python3
"""Build the card's contents: an MBR with one FAT16 volume on it (ADR 0033).

Written out rather than shipped, so what is on the card is readable as code.
The layout is the plain one every FAT16 volume has, in this order:

    sector 0            master boot record, one partition entry
    ...                 gap, so the volume starts on a comfortable boundary
    volume + 0          boot sector, carrying the BIOS parameter block
    volume + 1          two copies of the file allocation table
    ...                 the root directory, a fixed number of entries
    ...                 the data area, in clusters

Only the fields a driver actually reads are filled in. The rest are the values
a formatter conventionally writes, which is what makes a volume look ordinary
to code that sanity-checks it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

SECTOR = 512
PARTITION_LBA = 2048            # 1 MiB in, where formatters have put it for years
RESERVED_SECTORS = 1
FAT_COPIES = 2
ROOT_ENTRIES = 512
SECTORS_PER_CLUSTER = 4

MEDIA_FIXED = 0xF8
PARTITION_TYPE_FAT16 = 0x06
BOOT_SIGNATURE = 0xAA55

# The first two FAT entries are not clusters: entry 0 records the media type
# and entry 1 is an end-of-chain marker kept for historical reasons.
FIRST_DATA_CLUSTER = 2
END_OF_CHAIN = 0xFFFF

ATTR_ARCHIVE = 0x20
ATTR_VOLUME_LABEL = 0x08


@dataclass(frozen=True)
class File:
    """A file to place in the root directory. `name` is 8.3, upper case."""

    name: str
    data: bytes


def _short_name(name: str) -> bytes:
    stem, _, suffix = name.partition(".")
    return (stem[:8].ljust(8) + suffix[:3].ljust(3)).upper().encode("ascii")


def _boot_sector(total_sectors: int, sectors_per_fat: int, label: str) -> bytes:
    boot = bytearray(SECTOR)
    boot[0:3] = b"\xeb\x3c\x90"                     # a jump, as a formatter writes
    boot[3:11] = b"BOARDEMU"                        # OEM name, any eight bytes
    struct.pack_into("<H", boot, 11, SECTOR)        # bytes per sector
    boot[13] = SECTORS_PER_CLUSTER
    struct.pack_into("<H", boot, 14, RESERVED_SECTORS)
    boot[16] = FAT_COPIES
    struct.pack_into("<H", boot, 17, ROOT_ENTRIES)
    # Total sectors goes in the 16-bit field when it fits, and in the 32-bit
    # one otherwise. Drivers check that exactly one of them is set.
    if total_sectors < 0x10000:
        struct.pack_into("<H", boot, 19, total_sectors)
    else:
        struct.pack_into("<I", boot, 32, total_sectors)
    boot[21] = MEDIA_FIXED
    struct.pack_into("<H", boot, 22, sectors_per_fat)
    struct.pack_into("<H", boot, 24, 63)            # sectors per track
    struct.pack_into("<H", boot, 26, 255)           # heads
    struct.pack_into("<I", boot, 28, PARTITION_LBA)  # sectors before this volume
    boot[36] = 0x80                                 # drive number
    boot[38] = 0x29                                 # an extended block follows
    struct.pack_into("<I", boot, 39, 0x1234ABCD)    # volume serial
    boot[43:54] = label[:11].ljust(11).upper().encode("ascii")
    boot[54:62] = b"FAT16   "
    struct.pack_into("<H", boot, 510, BOOT_SIGNATURE)
    return bytes(boot)


def _master_boot_record(total_sectors: int) -> bytes:
    mbr = bytearray(SECTOR)
    entry = 446
    mbr[entry] = 0x80                               # bootable, conventionally
    mbr[entry + 1:entry + 4] = b"\x00\x02\x00"      # start, in the old geometry
    mbr[entry + 4] = PARTITION_TYPE_FAT16
    mbr[entry + 5:entry + 8] = b"\xff\xff\xff"      # end, likewise
    struct.pack_into("<I", mbr, entry + 8, PARTITION_LBA)
    struct.pack_into("<I", mbr, entry + 12, total_sectors)
    struct.pack_into("<H", mbr, 510, BOOT_SIGNATURE)
    return bytes(mbr)


def build(size_mb: int = 32, label: str = "BOARDEMUL",
          files: tuple[File, ...] = ()) -> bytearray:
    """Lay out a card of `size_mb` with one FAT16 volume holding `files`."""
    total = (size_mb * 1024 * 1024) // SECTOR
    volume_sectors = total - PARTITION_LBA
    clusters = volume_sectors // SECTORS_PER_CLUSTER
    # Two bytes per entry, rounded up to whole sectors, and the entries for the
    # clusters that exist plus the two reserved ones.
    sectors_per_fat = ((clusters + FIRST_DATA_CLUSTER) * 2 + SECTOR - 1) // SECTOR

    image = bytearray(total * SECTOR)
    image[0:SECTOR] = _master_boot_record(volume_sectors)

    volume = PARTITION_LBA * SECTOR
    image[volume:volume + SECTOR] = _boot_sector(volume_sectors, sectors_per_fat, label)

    fat = bytearray(sectors_per_fat * SECTOR)
    struct.pack_into("<H", fat, 0, 0xFF00 | MEDIA_FIXED)
    struct.pack_into("<H", fat, 2, END_OF_CHAIN)

    root_start = volume + (RESERVED_SECTORS + FAT_COPIES * sectors_per_fat) * SECTOR
    data_start = root_start + ROOT_ENTRIES * 32
    cluster_bytes = SECTORS_PER_CLUSTER * SECTOR

    root = bytearray(ROOT_ENTRIES * 32)
    slot = 0
    if label:
        root[0:11] = label[:11].ljust(11).upper().encode("ascii")
        root[11] = ATTR_VOLUME_LABEL
        slot = 1

    cluster = FIRST_DATA_CLUSTER
    for item in files:
        span = max(1, (len(item.data) + cluster_bytes - 1) // cluster_bytes)
        entry = slot * 32
        root[entry:entry + 11] = _short_name(item.name)
        root[entry + 11] = ATTR_ARCHIVE
        struct.pack_into("<H", root, entry + 26, cluster)
        struct.pack_into("<I", root, entry + 28, len(item.data))
        for step in range(span):
            here = cluster + step
            following = END_OF_CHAIN if step == span - 1 else here + 1
            struct.pack_into("<H", fat, here * 2, following)
        at = data_start + (cluster - FIRST_DATA_CLUSTER) * cluster_bytes
        image[at:at + len(item.data)] = item.data
        cluster += span
        slot += 1

    for copy in range(FAT_COPIES):
        at = volume + (RESERVED_SECTORS + copy * sectors_per_fat) * SECTOR
        image[at:at + len(fat)] = fat
    image[root_start:root_start + len(root)] = root
    return image
