#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Flash image and eFuse tests.

These run against the real vendor binary, so they double as a check that the
file on disk is the one `firmware/MANIFEST.toml` pins.
"""

from __future__ import annotations

import hashlib
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import FIRMWARE, REPO_ROOT  # noqa: E402
from tools import mkflash  # noqa: E402

pytestmark = pytest.mark.skipif(not FIRMWARE.exists(),
                                reason="vendor firmware is not vendored; see MANIFEST.toml")


@pytest.fixture(scope="module")
def image() -> bytes:
    return FIRMWARE.read_bytes()


def test_file_matches_the_pinned_hash(image: bytes) -> None:
    manifest = tomllib.loads((REPO_ROOT / "firmware" / "MANIFEST.toml").read_text())
    entry = next(e for e in manifest["image"] if e["name"] == "factory")
    assert hashlib.sha256(image).hexdigest() == entry["sha256"]
    assert len(image) == entry["size"]


def test_bootloader_header_is_an_esp32s3_image(image: bytes) -> None:
    boot = mkflash.parse_image(image, 0)
    assert boot.chip_id == mkflash.CHIP_ID_ESP32S3
    assert boot.checksum_ok
    assert boot.sha256_ok
    assert boot.flash_size_mb == 16


def test_bootloader_segments_are_all_internal_ram(image: bytes) -> None:
    """It runs before the flash cache is configured, so it cannot execute from
    cache-mapped flash."""
    boot = mkflash.parse_image(image, 0)
    assert all("SRAM" in s.region for s in boot.segments)


def test_partition_table_is_found_by_scanning(image: bytes) -> None:
    """Its offset is a compile-time constant no header records. It happens to be
    0x8000 here; the point is that nothing hardcoded it."""
    offset, parts = mkflash.find_partition_table(image)
    assert offset == 0x8000
    assert {p.label for p in parts} >= {"nvs", "otadata", "app0", "spiffs"}


def test_partition_table_rejects_a_corrupted_md5(image: bytes) -> None:
    broken = bytearray(image)
    broken[0x8000 + 4] ^= 0xFF          # perturb the first entry's offset
    with pytest.raises(mkflash.ImageError):
        mkflash.parse_partition_table(bytes(broken), 0x8000)


def test_partitions_tile_the_flash_without_overlap(image: bytes) -> None:
    boot = mkflash.parse_image(image, 0)
    _, parts = mkflash.find_partition_table(image)
    assert mkflash.check_partition_layout(parts, boot.flash_size_mb << 20) == []


def test_app_image_is_intact_and_needs_no_chip_revision(image: bytes) -> None:
    _, parts = mkflash.find_partition_table(image)
    app_part = min((p for p in parts if p.ptype == 0), key=lambda p: p.offset)
    app = mkflash.parse_image(image, app_part.offset)
    assert app.checksum_ok and app.sha256_ok
    # An all-zero eFuse blob reports revision 0, so anything above that would be
    # refused by the bootloader.
    assert app.min_chip_rev == 0


def test_app_descriptor_identifies_the_build(image: bytes) -> None:
    _, parts = mkflash.find_partition_table(image)
    app_part = min((p for p in parts if p.ptype == 0), key=lambda p: p.offset)
    desc = mkflash.read_app_descriptor(image, mkflash.parse_image(image, app_part.offset))
    assert desc is not None
    assert desc["idf_ver"].startswith("v4.4")


def test_a_truncated_image_is_rejected(image: bytes) -> None:
    with pytest.raises(mkflash.ImageError):
        mkflash.parse_image(image[:0x40], 0)


def test_a_bad_magic_byte_is_rejected(image: bytes) -> None:
    broken = bytearray(image)
    broken[0] = 0x00
    with pytest.raises(mkflash.ImageError):
        mkflash.parse_image(bytes(broken), 0)


# -- eFuse ------------------------------------------------------------------


def test_mac_is_stored_reversed_in_block1() -> None:
    """ESP-IDF's eFuse table defines the MAC as BLK1 bits 40,32,24,16,8,0 -- in
    that order -- so the octets sit backwards relative to the word layout.
    Storing them forwards yields a reversed MAC in the guest.
    """
    blob = mkflash.build_efuse(mkflash.parse_mac("02:54:44:4D:00:01"))
    assert len(blob) == mkflash.EFUSE_FILE_SIZE
    block1 = blob[mkflash.EFUSE_BLOCK1_OFFSET:mkflash.EFUSE_BLOCK1_OFFSET + 6]
    assert block1 == bytes([0x01, 0x00, 0x4D, 0x44, 0x54, 0x02])


def test_efuse_is_otherwise_blank() -> None:
    """A virgin chip: no secure boot, no flash encryption, revision zero."""
    blob = mkflash.build_efuse(mkflash.parse_mac("02:54:44:4D:00:01"))
    assert blob[:mkflash.EFUSE_BLOCK1_OFFSET] == bytes(mkflash.EFUSE_BLOCK1_OFFSET)


def test_default_mac_is_locally_administered() -> None:
    """It is not a real device, and on a network it must not claim to be."""
    assert mkflash.parse_mac(mkflash.DEFAULT_MAC)[0] & 0x02


@pytest.mark.parametrize("bad", ["", "02:54:44", "zz:54:44:4d:00:01"])
def test_malformed_macs_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        mkflash.parse_mac(bad)
