#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Start BoardEmul: one command, from a clone (ADR 0159).

    python boardemul.py              # check, build what is missing, open the page
    python boardemul.py --check      # say what this machine lacks, and stop
    python boardemul.py --port 8420 --no-open

On a host whose runs go into a container (Windows; ADR 0140), it uses the
runtime image, which carries QEMU already built, and builds that image the
first time. On a host that runs natively and has QEMU built, it uses that.
It fetches the vendor firmware the manifest pins, then serves the page.

Developers keep ADR 0147's commands: the development image, tools/build_qemu.sh
and tools/fetch_firmware.py, which this calls too.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.doctor import PUBLISHED_IMAGE, RUNTIME_IMAGE  # noqa: E402


def say(text: str) -> None:
    print(f"boardemul: {text}", flush=True)


def build_runtime_image(why: str) -> bool:
    from tools import doctor
    say(f"building the {RUNTIME_IMAGE} image ({why}): it compiles QEMU, which "
        "takes ten to thirty minutes and downloads its sources from GitHub")
    done = subprocess.run(["docker", "build", "-t", RUNTIME_IMAGE,
                           "--build-arg", f"BOARDEMUL_SOURCES={doctor.sources_digest()}",
                           "-f", "docker/runtime/Dockerfile", "."], cwd=REPO_ROOT)
    return done.returncode == 0


def pull_runtime_image() -> bool:
    """An installed BoardEmul runs the image CI published for its version
    (ADR 0183)."""
    say(f"pulling the {PUBLISHED_IMAGE} image, which carries QEMU")
    return subprocess.run(["docker", "pull", PUBLISHED_IMAGE]).returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="store_true", help="say the version, and stop")
    ap.add_argument("--check", action="store_true",
                    help="say what this machine lacks, and stop")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--no-open", action="store_true", help="do not open a browser tab")
    args = ap.parse_args(argv)
    from emu.version import VERSION
    if args.version:
        print(f"BoardEmul {VERSION}")
        return 0

    if sys.version_info < (3, 11):
        say(f"Python {sys.version.split()[0]} is too old: install Python 3.12")
        return 1

    from emu import device as device_folder
    from tools import doctor, fetch_firmware

    # Runs go into the runtime image unless the host runs them natively with
    # a QEMU of its own (ADR 0159).
    native = device_folder.runs_natively()
    if not native and not os.environ.get("BOARDEMUL_IMAGE"):
        os.environ["BOARDEMUL_IMAGE"] = RUNTIME_IMAGE
    image = os.environ.get("BOARDEMUL_IMAGE", device_folder.DEFAULT_IMAGE)

    findings = doctor.diagnose()
    blocking = [f for f in findings if f.level == doctor.ERROR]
    if args.check:
        doctor.main([])
        return 1 if blocking else 0

    for f in blocking:
        if f.what == "Build image" and image == RUNTIME_IMAGE:
            if image == PUBLISHED_IMAGE:
                if not pull_runtime_image():
                    say("the image did not arrive; Docker's output above says why")
                    return 1
                continue
            why = ("its QEMU sources have changed since it was built"
                   if "other QEMU sources" in f.detail else "once")
            if not build_runtime_image(why):
                say("the image did not build; its output above says why")
                return 1
        elif f.what.startswith("Firmware"):
            continue                    # fetched below
        else:
            say(f"{f.what}: {f.detail}" + (f" -- fix: {f.fix}" if f.fix else ""))
            return 1

    missing = [e["name"] for e in fetch_firmware.load()
               if fetch_firmware.state(e) == "missing"]
    if missing:
        say(f"fetching {len(missing)} pinned firmware image(s)")
        fetch_firmware.main([arg for name in missing for arg in ("--only", name)])

    still = [f for f in doctor.diagnose() if f.level == doctor.ERROR]
    if still:
        for f in still:
            say(f"{f.what}: {f.detail}" + (f" -- fix: {f.fix}" if f.fix else ""))
        return 1

    say(f"ready; runs start {'on this host' if native else 'in ' + image}")
    from emu import web
    return web.main(["--port", str(args.port)] + (["--no-open"] if args.no_open else []))


if __name__ == "__main__":
    sys.exit(main())
