#!/usr/bin/env python3
"""The firmware fetcher takes only what the manifest pins (ADR 0147).

Fast: the downloads come from a function standing in for the network.
"""

from __future__ import annotations

import hashlib
import io
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import fetch_firmware  # noqa: E402
from tools import doctor  # noqa: E402

IMAGE = b"\xe9" + b"image bytes" * 100


def entry(data: bytes = IMAGE, **more) -> dict:
    return {"name": "demo", "path": "sub/demo.bin", "url": "https://example/demo.bin",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), **more}


def serving(table: dict[str, bytes]):
    asked = []

    def get(url: str) -> bytes:
        asked.append(url)
        return table[url]
    get.asked = asked
    return get


def test_a_missing_image_is_fetched_and_checked(tmp_path: Path) -> None:
    get = serving({"https://example/demo.bin": IMAGE})
    assert fetch_firmware.fetch(entry(), tmp_path, get) == "fetched"
    assert (tmp_path / "sub" / "demo.bin").read_bytes() == IMAGE
    assert not (tmp_path / "sub" / "demo.bin.part").exists()


def test_an_image_already_right_is_not_fetched_again(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "demo.bin").write_bytes(IMAGE)
    get = serving({})
    assert fetch_firmware.fetch(entry(), tmp_path, get) == "ok"
    assert get.asked == []


def test_a_download_that_is_not_the_pinned_image_leaves_nothing(tmp_path: Path) -> None:
    get = serving({"https://example/demo.bin": IMAGE + b"tampered"})
    outcome = fetch_firmware.fetch(entry(), tmp_path, get)
    assert outcome.startswith("refused")
    assert not (tmp_path / "sub" / "demo.bin").exists()
    assert not (tmp_path / "sub" / "demo.bin.part").exists()


def test_a_wrong_file_on_disk_is_reported_and_kept(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "demo.bin").write_bytes(b"someone else's")
    get = serving({})
    assert fetch_firmware.fetch(entry(), tmp_path, get) == "wrong"
    assert (tmp_path / "sub" / "demo.bin").read_bytes() == b"someone else's"
    assert get.asked == []


def test_an_archive_member_is_taken_out_of_its_zip(tmp_path: Path) -> None:
    packed = io.BytesIO()
    with zipfile.ZipFile(packed, "w") as archive:
        archive.writestr("other.bin", b"not this")
        archive.writestr("littlefs.bin", IMAGE)
    pinned = entry(archive="https://example/release.zip", member="littlefs.bin")
    del pinned["url"]
    get = serving({"https://example/release.zip": packed.getvalue()})
    assert fetch_firmware.fetch(pinned, tmp_path, get) == "fetched"
    assert (tmp_path / "sub" / "demo.bin").read_bytes() == IMAGE


def test_every_pinned_image_names_a_commit_not_a_branch() -> None:
    """A repository's firmware page gets rewritten; a commit keeps serving
    the bytes the hash pins (ADR 0147)."""
    manifest = tomllib.loads(fetch_firmware.MANIFEST.read_text(encoding="utf-8"))
    for image in manifest["image"]:
        url = image.get("url") or image["archive"]
        assert "/master/" not in url and "/main/" not in url, image["name"]
        assert {"path", "size", "sha256"} <= image.keys()


def test_the_doctor_says_a_missing_image_is_only_a_warning(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(fetch_firmware, "FIRMWARE", tmp_path)
    monkeypatch.setattr(fetch_firmware, "state",
                        lambda e, root=tmp_path: "missing")
    findings = doctor.check_firmware()
    assert findings and all(f.level == doctor.WARN for f in findings)
    assert all("fetch_firmware.py --only" in f.fix for f in findings)
