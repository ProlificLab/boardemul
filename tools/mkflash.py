#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Build a QEMU-ready flash image and eFuse blob from a vendor firmware image.

See docs/adr/0003-flash-image-assembly.md for why this parses rather than
assumes, and docs/BOOT.md for the formats involved.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

IMAGE_MAGIC = 0xE9
PARTITION_MAGIC = b"\xaa\x50"
PARTITION_MD5_MAGIC = b"\xeb\xeb"
APP_DESC_MAGIC = 0xABCD5432
CHIP_ID_ESP32S3 = 9

# The XOR seed the ROM and bootloader use for the trailing checksum byte.
CHECKSUM_SEED = 0xEF

# The table is not self-locating: its offset is a compile-time constant baked
# into the bootloader that no header records. Scanning is the only honest way
# to find it. 1 KiB steps because that is the alignment the tooling guarantees.
PARTITION_SCAN_STEP = 0x1000
PARTITION_SCAN_LIMIT = 0x100000
PARTITION_TABLE_SIZE = 0xC00

# QEMU's ESP32-S3 machine accepts only these flash sizes.
QEMU_FLASH_SIZES = (2 << 20, 4 << 20, 8 << 20, 16 << 20)

# Flash size encoded in the high nibble of image header byte 3.
FLASH_SIZE_NIBBLE = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16, 5: 32, 6: 64, 7: 128}

SPI_MODES = {0: "QIO", 1: "QOUT", 2: "DIO", 3: "DOUT", 4: "FAST_READ", 5: "SLOW_READ"}

# ESP32-S3 address regions, used to sanity-check segment load addresses. A
# segment landing outside all of these means the image is not what it claims.
REGIONS = (
    (0x3C000000, 0x3E000000, "DROM (flash, cached)"),
    (0x3FC88000, 0x3FD00000, "DRAM (internal SRAM)"),
    (0x40370000, 0x403E0000, "IRAM (internal SRAM)"),
    (0x42000000, 0x44000000, "IROM (flash, cached)"),
    (0x50000000, 0x50002000, "RTC SLOW"),
    (0x600FE000, 0x60100000, "RTC FAST"),
)

PARTITION_TYPES = {0: "app", 1: "data"}
PARTITION_SUBTYPES = {
    (0, 0x00): "factory", (0, 0x10): "ota_0", (0, 0x11): "ota_1",
    (1, 0x00): "otadata", (1, 0x01): "phy", (1, 0x02): "nvs",
    (1, 0x04): "nvs_keys", (1, 0x81): "fat", (1, 0x82): "spiffs",
    (1, 0x83): "littlefs",
}

DEFAULT_MAC = "02:54:44:4D:00:01"
EFUSE_FILE_SIZE = 1024
# The fork reads the file straight into a flat ESPEfuseBlocks struct: BLOCK0 is
# six 32-bit words, so BLOCK1 -- which carries the MAC -- starts here.
EFUSE_BLOCK1_OFFSET = 6 * 4


class ImageError(Exception):
    """Raised when an image is malformed. Never swallowed: a bad image that
    boots halfway looks exactly like missing hardware, which is the confusion
    this whole milestone exists to prevent."""


@dataclass(frozen=True)
class Segment:
    load_addr: int
    length: int
    file_offset: int

    @property
    def region(self) -> str:
        for lo, hi, name in REGIONS:
            if lo <= self.load_addr < hi:
                return name
        return "UNKNOWN"


@dataclass(frozen=True)
class Image:
    offset: int
    entry_addr: int
    chip_id: int
    spi_mode: int
    flash_size_mb: int
    wp_pin: int
    min_chip_rev: int
    max_chip_rev: int
    hash_appended: bool
    segments: tuple[Segment, ...]
    end_offset: int
    checksum_ok: bool
    sha256_ok: bool | None


@dataclass(frozen=True)
class Partition:
    label: str
    ptype: int
    subtype: int
    offset: int
    size: int

    @property
    def type_name(self) -> str:
        return PARTITION_TYPES.get(self.ptype, f"0x{self.ptype:02x}")

    @property
    def subtype_name(self) -> str:
        return PARTITION_SUBTYPES.get((self.ptype, self.subtype), f"0x{self.subtype:02x}")


def parse_image(data: bytes, offset: int) -> Image:
    if offset + 24 > len(data):
        raise ImageError(f"no room for an image header at 0x{offset:X}")
    if data[offset] != IMAGE_MAGIC:
        raise ImageError(f"bad image magic at 0x{offset:X}: "
                         f"0x{data[offset]:02X}, expected 0x{IMAGE_MAGIC:02X}")

    seg_count, spi_mode, spi_size_speed = data[offset + 1:offset + 4]
    entry_addr, = struct.unpack_from("<I", data, offset + 4)
    wp_pin = data[offset + 8]
    chip_id, = struct.unpack_from("<H", data, offset + 12)
    min_rev, = struct.unpack_from("<H", data, offset + 15)
    max_rev, = struct.unpack_from("<H", data, offset + 17)
    hash_appended = bool(data[offset + 23])

    if chip_id != CHIP_ID_ESP32S3:
        raise ImageError(f"image at 0x{offset:X} declares chip id {chip_id}, "
                         f"expected {CHIP_ID_ESP32S3} (ESP32-S3)")

    segments = []
    cur = offset + 24
    checksum = CHECKSUM_SEED
    for i in range(seg_count):
        if cur + 8 > len(data):
            raise ImageError(f"segment {i} header runs past end of file")
        load_addr, length = struct.unpack_from("<II", data, cur)
        cur += 8
        if cur + length > len(data):
            raise ImageError(f"segment {i} data ({length} bytes at 0x{cur:X}) "
                             f"runs past end of file")
        seg = Segment(load_addr, length, cur - offset)
        if seg.region == "UNKNOWN":
            raise ImageError(f"segment {i} loads at 0x{load_addr:08X}, which is "
                             f"in no valid ESP32-S3 region")
        for b in data[cur:cur + length]:
            checksum ^= b
        segments.append(seg)
        cur += length

    # The checksum byte sits at the next offset where the image length becomes
    # a multiple of 16, with zero padding in between.
    pad_end = cur
    while (pad_end - offset + 1) % 16 != 0:
        pad_end += 1
    if pad_end >= len(data):
        raise ImageError("no room for the checksum byte")
    checksum_ok = data[pad_end] == checksum
    end = pad_end + 1

    sha256_ok: bool | None = None
    if hash_appended:
        if end + 32 > len(data):
            raise ImageError("header claims an appended SHA-256 but there is no room for it")
        sha256_ok = hashlib.sha256(data[offset:end]).digest() == data[end:end + 32]
        end += 32

    return Image(
        offset=offset, entry_addr=entry_addr, chip_id=chip_id, spi_mode=spi_mode,
        flash_size_mb=FLASH_SIZE_NIBBLE.get(spi_size_speed >> 4, 0), wp_pin=wp_pin,
        min_chip_rev=min_rev, max_chip_rev=max_rev, hash_appended=hash_appended,
        segments=tuple(segments), end_offset=end,
        checksum_ok=checksum_ok, sha256_ok=sha256_ok,
    )


def find_partition_table(data: bytes) -> tuple[int, tuple[Partition, ...]]:
    limit = min(len(data), PARTITION_SCAN_LIMIT)
    for base in range(0, limit, PARTITION_SCAN_STEP):
        if data[base:base + 2] != PARTITION_MAGIC:
            continue
        try:
            parts = parse_partition_table(data, base)
        except ImageError:
            # A stray 0xAA50 that fails MD5 is not the table. Keep scanning
            # rather than reporting a false positive.
            continue
        return base, parts
    raise ImageError("no valid partition table found "
                     f"in the first 0x{limit:X} bytes")


def parse_partition_table(data: bytes, base: int) -> tuple[Partition, ...]:
    parts: list[Partition] = []
    for i in range(PARTITION_TABLE_SIZE // 32):
        entry = data[base + i * 32: base + i * 32 + 32]
        if len(entry) < 32:
            raise ImageError("partition table runs past end of file")
        if entry[:2] == PARTITION_MD5_MAGIC:
            want = hashlib.md5(data[base:base + i * 32]).digest()
            if entry[16:32] != want:
                raise ImageError("partition table MD5 mismatch")
            return tuple(parts)
        if entry[:2] != PARTITION_MAGIC:
            raise ImageError("partition table ended without an MD5 entry")
        ptype, subtype = entry[2], entry[3]
        offset, size = struct.unpack_from("<II", entry, 4)
        label = entry[12:28].split(b"\x00")[0].decode("ascii", "replace")
        parts.append(Partition(label, ptype, subtype, offset, size))
    raise ImageError("partition table has no terminator")


def check_partition_layout(parts: tuple[Partition, ...], flash_size: int) -> list[str]:
    problems = []
    ordered = sorted(parts, key=lambda p: p.offset)
    for a, b in zip(ordered, ordered[1:]):
        if a.offset + a.size > b.offset:
            problems.append(f"{a.label} overlaps {b.label}")
    for p in ordered:
        if p.offset + p.size > flash_size:
            problems.append(f"{p.label} extends past the end of a "
                            f"{flash_size >> 20} MB flash")
    return problems


def read_app_descriptor(data: bytes, image: Image) -> dict[str, str] | None:
    """The descriptor lives at the start of the first segment, which puts it 32
    bytes into the image. It identifies the build far more precisely than any
    filename does."""
    o = image.offset + 24 + 8
    magic, = struct.unpack_from("<I", data, o)
    if magic != APP_DESC_MAGIC:
        return None
    field = lambda a, n: data[o + a:o + a + n].split(b"\x00")[0].decode("utf-8", "replace")
    return {
        "version": field(16, 32),
        "project_name": field(48, 32),
        "time": field(80, 16),
        "date": field(96, 16),
        "idf_ver": field(112, 32),
    }


def parse_mac(text: str) -> bytes:
    parts = text.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"MAC must have six octets, got {len(parts)}")
    return bytes(int(p, 16) for p in parts)


def build_efuse(mac: bytes) -> bytes:
    """Zeroed eFuses (a virgin chip: no secure boot, no flash encryption, chip
    revision 0) apart from the factory MAC in BLOCK1.

    The byte order below looks wrong and is not. ESP-IDF's esp_efuse_table.csv
    for esp32s3 defines MAC as BLK1 bits 40, 32, 24, 16, 8, 0 -- in that order
    -- so mac[0] occupies the *highest* of the six bit positions. Storing it
    forwards would produce a reversed MAC in the guest.
    """
    blob = bytearray(EFUSE_FILE_SIZE)
    word0 = mac[5] | mac[4] << 8 | mac[3] << 16 | mac[2] << 24
    word1 = mac[1] | mac[0] << 8
    struct.pack_into("<II", blob, EFUSE_BLOCK1_OFFSET, word0, word1)
    return bytes(blob)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware", type=Path, help="merged vendor flash image")
    ap.add_argument("-o", "--outdir", type=Path, default=Path("build"))
    ap.add_argument("--mac", default=DEFAULT_MAC,
                    help=f"factory MAC to burn into eFuse BLOCK1 (default {DEFAULT_MAC})")
    ap.add_argument("--overlay", action="append", default=[], metavar="OFFSET=FILE",
                    help="splice FILE into the image at OFFSET, which must lie inside "
                         "one data partition -- what a vendor installer does with a "
                         "filesystem image (ADR 0021)")
    args = ap.parse_args(argv)

    data = args.firmware.read_bytes()
    print(f"input   : {args.firmware}")
    print(f"          {len(data)} bytes (0x{len(data):X})")
    print(f"          sha256 {hashlib.sha256(data).hexdigest()}")

    try:
        boot = parse_image(data, 0)
        table_offset, parts = find_partition_table(data)
    except ImageError as e:
        print(f"\nFAIL: {e}", file=sys.stderr)
        return 1

    flash_size = boot.flash_size_mb << 20
    print(f"\nbootloader @ 0x0")
    print(f"  entry 0x{boot.entry_addr:08X}  {SPI_MODES.get(boot.spi_mode, '?')} "
          f"{boot.flash_size_mb}MB  wp_pin 0x{boot.wp_pin:02X}")
    for i, s in enumerate(boot.segments):
        print(f"  seg{i}  load=0x{s.load_addr:08X}  len={s.length:7d}  {s.region}")
    print(f"  checksum {'ok' if boot.checksum_ok else 'BAD'}"
          f"   sha256 {_hash_state(boot)}")

    print(f"\npartition table @ 0x{table_offset:X}  (MD5 verified)")
    for p in parts:
        print(f"  {p.label:<10} {p.type_name:<5} {p.subtype_name:<9} "
              f"0x{p.offset:<8X} 0x{p.size:X}")
    for problem in check_partition_layout(parts, flash_size):
        print(f"  WARNING: {problem}")

    app_parts = [p for p in parts if p.ptype == 0]
    if not app_parts:
        print("\nFAIL: partition table declares no app partition", file=sys.stderr)
        return 1
    app_part = min(app_parts, key=lambda p: p.offset)

    try:
        app = parse_image(data, app_part.offset)
    except ImageError as e:
        print(f"\nFAIL: app partition '{app_part.label}': {e}", file=sys.stderr)
        return 1

    print(f"\napp '{app_part.label}' @ 0x{app_part.offset:X}")
    print(f"  entry 0x{app.entry_addr:08X}  min_chip_rev {app.min_chip_rev}  "
          f"max_chip_rev 0x{app.max_chip_rev:04X}")
    for i, s in enumerate(app.segments):
        print(f"  seg{i}  load=0x{s.load_addr:08X}  len={s.length:7d}  {s.region}")
    print(f"  checksum {'ok' if app.checksum_ok else 'BAD'}"
          f"   sha256 {_hash_state(app)}")

    desc = read_app_descriptor(data, app)
    if desc:
        print(f"  built by {desc['project_name']!r} on {desc['idf_ver']!r} "
              f"at {desc['time']} {desc['date']}")

    failures = [name for name, ok in
                (("bootloader checksum", boot.checksum_ok),
                 ("bootloader sha256", boot.sha256_ok),
                 ("app checksum", app.checksum_ok),
                 ("app sha256", app.sha256_ok)) if ok is False]
    if failures:
        print(f"\nFAIL: integrity check failed: {', '.join(failures)}", file=sys.stderr)
        return 1

    if flash_size not in QEMU_FLASH_SIZES:
        accepted = ", ".join(f"{s >> 20}MB" for s in QEMU_FLASH_SIZES)
        print(f"\nFAIL: image declares {boot.flash_size_mb}MB flash; "
              f"QEMU accepts only {accepted}", file=sys.stderr)
        return 1
    if len(data) > flash_size:
        print(f"\nFAIL: input is larger than the {boot.flash_size_mb}MB flash "
              f"it declares", file=sys.stderr)
        return 1

    args.outdir.mkdir(parents=True, exist_ok=True)
    flash_path = args.outdir / "flash.bin"
    efuse_path = args.outdir / "efuse.bin"

    # 0xFF is erased-flash state; padding with zeros would make empty regions
    # look like written data to anything that inspects the image later.
    image = bytearray(data + b"\xff" * (flash_size - len(data)))
    for spec in args.overlay:
        offset_text, _, path_text = spec.partition("=")
        try:
            offset = int(offset_text, 0)
            blob = Path(path_text).read_bytes()
        except (ValueError, OSError) as e:
            print(f"\nFAIL: overlay {spec!r}: {e}", file=sys.stderr)
            return 1
        # Checked against the parsed table, not trusted: an overlay in the wrong
        # place would corrupt a run silently, and this tool exists to parse
        # rather than assume (ADR 0003).
        home = [q for q in parts if q.ptype == 1 and q.offset <= offset
                and offset + len(blob) <= q.offset + q.size]
        if not home:
            print(f"\nFAIL: overlay {path_text} at 0x{offset:X} ({len(blob)} bytes) "
                  f"does not fit inside any data partition", file=sys.stderr)
            return 1
        image[offset:offset + len(blob)] = blob
        print(f"overlay {path_text}  0x{offset:X}  {len(blob)} bytes  "
              f"-> partition '{home[0].label}'")
    flash_path.write_bytes(bytes(image))
    mac = parse_mac(args.mac)
    efuse_path.write_bytes(build_efuse(mac))

    print(f"\nwrote {flash_path}  {flash_size} bytes ({boot.flash_size_mb}MB, "
          f"0xFF-padded from 0x{len(data):X})")
    print(f"wrote {efuse_path}  {EFUSE_FILE_SIZE} bytes, MAC {args.mac}")
    return 0


def _hash_state(image: Image) -> str:
    if image.sha256_ok is None:
        return "not present"
    return "ok" if image.sha256_ok else "BAD"


if __name__ == "__main__":
    sys.exit(main())
