#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Build the restart probe and write the flash image its test boots (ADR 0123).

The probe restarts the chip once from each core, by esp_restart() and by
abort(). It is ours, not a vendor's, so nothing downloads it: this builds it
from src/ with PlatformIO, on the same ESP-IDF 4.4 as the vendor firmware
(espressif32 6.5.0, Arduino-ESP32 2.0.14), and merges the bootloader,
partition table and app into one image as the vendor images are.

Run it on a host with PlatformIO, not in the build container:

    python tools/restart_probe/build.py
    python3 -m pytest tests/test_restart_probe.py -m slow

The image is not byte-for-byte reproducible (the app records its build time),
which is why firmware/MANIFEST.toml does not pin it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "firmware" / "restart_probe.bin",
                    help="where to write the merged flash image "
                         "(default: firmware/restart_probe.bin)")
    args = ap.parse_args(argv)
    return build_probe(HERE, args.out)


def build_probe(here: Path, out: Path) -> int:
    """Build the PlatformIO project in `here` and merge it into one flash
    image at `out`. Shared with the other probes (ADR 0142)."""
    subprocess.run([sys.executable, "-m", "platformio", "run", "-d", str(here)], check=True)

    pio = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    esptool = pio / "packages" / "tool-esptoolpy" / "esptool.py"
    boot_app0 = (pio / "packages" / "framework-arduinoespressif32" / "tools"
                 / "partitions" / "boot_app0.bin")
    build = here / ".pio" / "build" / "probe"
    for needed in (esptool, boot_app0):
        if not needed.is_file():
            raise SystemExit(f"missing {needed}; is PlatformIO's espressif32 platform installed?")

    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(esptool), "--chip", "esp32s3", "merge_bin",
                    "-o", str(out), "--flash_mode", "dio", "--flash_size", "16MB",
                    "0x0", str(build / "bootloader.bin"),
                    "0x8000", str(build / "partitions.bin"),
                    "0xe000", str(boot_app0),
                    "0x10000", str(build / "firmware.bin")], check=True)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
