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


# -- a firmware brings its filesystem (ADR 0150) ------------------------------


def _manifest(tmp_path: Path, firmware: bytes, littlefs: bytes) -> Path:
    def sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()
    path = tmp_path / "MANIFEST.toml"
    path.write_text(f'''
[[image]]
name = "mesh"
path = "mesh.bin"
url = "https://example/mesh.bin"
size = {len(firmware)}
sha256 = "{sha(firmware)}"
companions = ["fs"]

[[image]]
name = "fs"
path = "fs.bin"
url = "https://example/fs.bin"
offset = 0xc90000
size = {len(littlefs)}
sha256 = "{sha(littlefs)}"
''', encoding="utf-8")
    return path


def test_a_pinned_firmware_brings_its_companion(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, IMAGE, b"littlefs")
    (tmp_path / "fs.bin").write_bytes(b"littlefs")
    firmware = tmp_path / "elsewhere.bin"            # an upload, by its hash
    firmware.write_bytes(IMAGE)
    overlays, missing = fetch_firmware.companion_overlays(firmware, tmp_path, manifest)
    assert overlays == [f"0xc90000={(tmp_path / 'fs.bin').resolve().as_posix()}"]
    assert missing == []


def test_a_companion_not_fetched_is_named(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, IMAGE, b"littlefs")
    (tmp_path / "fs.bin").write_bytes(b"not the pinned one")
    firmware = tmp_path / "mesh.bin"
    firmware.write_bytes(IMAGE)
    assert fetch_firmware.companion_overlays(firmware, tmp_path, manifest) == ([], ["fs"])


def test_an_unpinned_firmware_brings_nothing(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, IMAGE, b"littlefs")
    firmware = tmp_path / "other.bin"
    firmware.write_bytes(b"another build")
    assert fetch_firmware.companion_overlays(firmware, tmp_path, manifest) == ([], [])


def test_the_meshtastic_build_names_its_filesystem() -> None:
    manifest = tomllib.loads(fetch_firmware.MANIFEST.read_text(encoding="utf-8"))
    by_name = {e["name"]: e for e in manifest["image"]}
    assert by_name["tdeck-meshtastic"]["companions"] == ["meshtastic-littlefs"]
    assert by_name["meshtastic-littlefs"]["offset"] == 0xC90000


def test_a_new_device_takes_the_companions_unless_given_overlays(tmp_path, monkeypatch):
    from emu import device as device_folder
    seen = []

    def build(firmware, outdir, mac, overlays):
        outdir.mkdir(parents=True, exist_ok=True)
        seen.append(list(overlays))
        (outdir / "flash.bin").write_bytes(b"f")
        (outdir / "efuse.bin").write_bytes(b"e")
        return outdir / "flash.bin", outdir / "efuse.bin"

    monkeypatch.setattr(fetch_firmware, "companion_overlays",
                        lambda fw: (["0xc90000=/fs.bin"], []))
    firmware = tmp_path / "fw.bin"
    firmware.write_bytes(IMAGE)
    made = device_folder.create(tmp_path / "d", "a", firmware, build=build)
    assert made.overlays == ("0xc90000=/fs.bin",)
    device_folder.create(tmp_path / "d", "b", firmware, overlays=("0x1=/x.bin",),
                         build=build)
    assert seen == [["0xc90000=/fs.bin"], ["0x1=/x.bin"]]
