#!/usr/bin/env python3
"""Build the matrix reset probe and write the flash image its test boots
(ADR 0142).

The probe raises a GPIO interrupt with the CPU's interrupt masked, resets the
digital system, and says whether a new edge on the same pin reaches the CPU
after it. Built as tools/restart_probe is, by its builder, on a host with
PlatformIO, not in the build container:

    python tools/matrix_reset_probe/build.py
    python3 -m pytest tests/test_matrix_reset_probe.py -m slow
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
                    default=REPO_ROOT / "firmware" / "matrix_reset_probe.bin",
                    help="where to write the merged flash image "
                         "(default: firmware/matrix_reset_probe.bin)")
    args = ap.parse_args(argv)
    spec = importlib.util.spec_from_file_location(
        "restart_probe_build", REPO_ROOT / "tools" / "restart_probe" / "build.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder.build_probe(HERE, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
