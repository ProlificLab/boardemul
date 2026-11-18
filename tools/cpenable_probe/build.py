#!/usr/bin/env python3
"""Build the CPENABLE probe and write the flash image its test boots
(ADR 0143).

The probe reads CPENABLE in a C++ global constructor, before FreeRTOS starts,
on a power-on boot, after esp_restart() and after a digital system reset.
Built as tools/restart_probe is, by its builder, on a host with PlatformIO,
not in the build container:

    python tools/cpenable_probe/build.py
    python3 -m pytest tests/test_cpenable_probe.py -m slow
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path,
                    default=REPO_ROOT / "firmware" / "cpenable_probe.bin",
                    help="where to write the merged flash image "
                         "(default: firmware/cpenable_probe.bin)")
    args = ap.parse_args(argv)
    spec = importlib.util.spec_from_file_location(
        "restart_probe_build", REPO_ROOT / "tools" / "restart_probe" / "build.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder.build_probe(HERE, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
