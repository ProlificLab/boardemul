#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Two Meshtastic devices, each in its own build container, on one named air
(ADR 0145). Run on a host that starts runs in the container (Windows), with
Docker running:

    python tools/air_between_containers.py

It makes two device folders in a scratch directory from the vendor's
Meshtastic build and a configured filesystem (tests/test_mesh.py builds one
into build/littlefs-configured.bin), gives them one air and two places on
it, starts both the way the web page does, and waits until either radio
says it heard a packet from the other. Exit 0 when one did.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from emu import device as device_folder  # noqa: E402

FIRMWARE = REPO_ROOT / "firmware" / "firmware-t-deck-tft-2.7.10.94d4bdf.bin"
CONFIGURED = REPO_ROOT / "build" / "littlefs-configured.bin"
OVERLAY_AT = 0xC90000
HEARD = "sx1262: heard a"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=900)
    ap.add_argument("--air", default="between")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folders")
    args = ap.parse_args(argv)
    for needed in (FIRMWARE, CONFIGURED):
        if not needed.is_file():
            raise SystemExit(f"missing {needed}")

    scratch = Path(tempfile.mkdtemp(prefix="air-", dir=REPO_ROOT / "logs"))
    devices = scratch / "devices"
    overlay = f"0x{OVERLAY_AT:x}={CONFIGURED}"
    handles = []
    try:
        for name, mac, at in (("one", "24:6F:28:00:00:11", "0,0"),
                              ("two", "24:6F:28:00:00:22", "50,0")):
            made = device_folder.create(devices, name, FIRMWARE, board="tdeck",
                                        mac=mac, overlays=(overlay,))
            made = device_folder.set_settings(
                made, ["--air", args.air, "--at", at, "--reboot"])
            handle = device_folder.RunHandle(made, logs=scratch / "logs")
            handle.start()
            handles.append(handle)
            print(f"started {name} in {handle.container or 'this host'}, "
                  f"output in {handle.outdir}")
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            for handle in handles:
                log = handle.outdir / f"{handle.device.name}.bus.log"
                text = log.read_text(errors="replace") if log.is_file() else ""
                if HEARD in text:
                    line = next(l for l in text.splitlines() if HEARD in l)
                    print(f"{handle.device.name}: {line.strip()}")
                    return 0
                if not handle.alive:
                    print(f"{handle.device.name} ended early:")
                    print("\n".join(handle.console_tail(20)))
                    return 1
            time.sleep(5)
        print(f"nothing heard in {args.seconds:.0f} s")
        return 1
    finally:
        for handle in handles:
            handle.stop()
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
