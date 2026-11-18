#!/usr/bin/env python3
"""Fetch the vendor firmware firmware/MANIFEST.toml pins, and check it (ADR 0147).

    python tools/fetch_firmware.py            # fetch whatever is missing
    python tools/fetch_firmware.py --check    # fetch nothing; say what is there
    python tools/fetch_firmware.py --only tdeck-meshtastic

Each image comes from its URL, or from a member of a zip archive when the
manifest names `archive` and `member`. It is written through a `.part` file
that becomes the image only once its size and SHA-256 match the manifest, so
a failed or tampered download never leaves a file a run would trust. An image
already present with the right hash is not fetched again; one present with
the wrong hash is reported and left alone.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = REPO_ROOT / "firmware"
MANIFEST = FIRMWARE / "MANIFEST.toml"
# A plain client name: nothing about whoever runs it goes upstream.
AGENT = "BoardEmul-fetch"
TIMEOUT_S = 120


def load(manifest: Path = MANIFEST) -> list[dict]:
    return tomllib.loads(manifest.read_text(encoding="utf-8"))["image"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def state(entry: dict, root: Path = FIRMWARE) -> str:
    """"ok", "missing", or "wrong" for an image on disk."""
    path = root / entry["path"]
    if not path.is_file():
        return "missing"
    data = path.read_bytes()
    return "ok" if len(data) == entry["size"] and sha256(data) == entry["sha256"] \
        else "wrong"


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return response.read()


def contents(entry: dict, get=download) -> bytes:
    """The image's bytes, from its URL or from its archive's member."""
    if "archive" in entry:
        with zipfile.ZipFile(io.BytesIO(get(entry["archive"]))) as archive:
            return archive.read(entry["member"])
    return get(entry["url"])


def fetch(entry: dict, root: Path = FIRMWARE, get=download) -> str:
    """Fetch one image unless it is already right. Returns what happened."""
    found = state(entry, root)
    if found == "ok":
        return "ok"
    if found == "wrong":
        return "wrong"
    data = contents(entry, get)
    if len(data) != entry["size"] or sha256(data) != entry["sha256"]:
        return (f"refused: got {len(data)} bytes, sha256 {sha256(data)[:16]}...; "
                f"the manifest pins {entry['size']}, {entry['sha256'][:16]}...")
    path = root / entry["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_bytes(data)
    part.replace(path)
    return "fetched"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="fetch nothing; say what is there and whether it is right")
    ap.add_argument("--only", metavar="NAME", action="append", default=[],
                    help="one image by its manifest name (repeatable)")
    args = ap.parse_args(argv)

    entries = load()
    names = {e["name"] for e in entries}
    unknown = [n for n in args.only if n not in names]
    if unknown:
        raise SystemExit(f"no image called {', '.join(unknown)}; the manifest has "
                         f"{', '.join(sorted(names))}")
    if args.only:
        entries = [e for e in entries if e["name"] in args.only]

    failed = 0
    for entry in entries:
        if args.check:
            outcome = state(entry)
        else:
            try:
                outcome = fetch(entry)
            except (OSError, zipfile.BadZipFile, KeyError) as exc:
                outcome = f"failed: {exc}"
        good = outcome in ("ok", "fetched")
        failed += not good
        print(f"{'ok' if good else '!!'} {entry['name']:22s} {outcome:9s} "
              f"firmware/{entry['path']}")
        if outcome == "wrong":
            print(f"    its hash is not the pinned one; remove it to fetch it again")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
