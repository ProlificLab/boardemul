# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""`boardemul test`: run a build on one device or several, run a test
command against them, and answer with its exit status (ADR 0182).

    boardemul test --build .pio/build/t-deck --board tdeck --devices 2 \\
        -- python -m pytest tests/

The command finds the page in BOARDEMUL_URL and the devices' names, comma
separated, in BOARDEMUL_DEVICES; `boardemul.Lab()` uses the first on its
own. Each device's logs are kept in --logs when the command ends.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from boardemul.build import Build, BuildError
from boardemul.lab import Lab, LabError


def test(argv: list[str]) -> int:
    if "--" not in argv:
        print("boardemul test: give the test command after --", file=sys.stderr)
        return 2
    cut = argv.index("--")
    own, command = argv[:cut], argv[cut + 1:]
    ap = argparse.ArgumentParser(prog="boardemul test")
    ap.add_argument("--build", required=True,
                    help="the build folder: firmware.bin, and its filesystem and "
                         "partitions.bin if it has one (a PlatformIO .pio/build/ENV)")
    ap.add_argument("--board", default="tdeck", help="tdeck or tdeck-max")
    ap.add_argument("--devices", type=int, default=1,
                    help="how many devices run the build; more than one share an air")
    ap.add_argument("--name", default="dut", help="the devices' name, numbered if several")
    ap.add_argument("--air", default="boardemul-test",
                    help="the LoRa air several devices share")
    ap.add_argument("--setting", action="append", default=[],
                    help="a run setting for every device, as the page takes them "
                         "(repeat: --setting=--host-clock)")
    ap.add_argument("--logs", default="boardemul-logs",
                    help="where each device's logs are kept")
    args = ap.parse_args(own)
    if not command:
        print("boardemul test: no test command after --", file=sys.stderr)
        return 2
    try:
        build = Build.from_dir(args.build)
    except BuildError as exc:
        print(f"boardemul test: {exc}", file=sys.stderr)
        return 2

    names = ([args.name] if args.devices == 1
             else [f"{args.name}{i}" for i in range(1, args.devices + 1)])
    with Lab() as lab:
        try:
            devices = [lab.device(n, build, board=args.board, settings=tuple(args.setting))
                       for n in names]
            for device in devices:
                device.start(air=args.air if len(devices) > 1 else None)
        except LabError as exc:
            print(f"boardemul test: {exc}", file=sys.stderr)
            return 2
        env = dict(os.environ, BOARDEMUL_URL=lab.url, BOARDEMUL_DEVICES=",".join(names))
        try:
            status = subprocess.run(command, env=env).returncode
        finally:
            logs = Path(args.logs)
            logs.mkdir(parents=True, exist_ok=True)
            for device in devices:
                device.stop()
                try:
                    device.save_logs(logs / f"{device.name}.zip")
                except LabError:
                    pass
    return status
