#!/usr/bin/env python3
"""Manage device folders: the VirtualBox ordinary case (ADR 0133).

    device.py create NAME --firmware F.bin [--board tdeck-max] [--mac ...]
    device.py list
    device.py show NAME
    device.py flash NAME --firmware F.bin [--overlay ...]
    device.py remove NAME
    device.py start NAME [run.py arguments...]

A device folder keeps identity and storage between runs. Starting one is
`emu/run.py --device`; this tool is for the folders themselves. Storage is
assembled once at create (or flash) and never rebuilt by a run.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from emu import device as device_folder  # noqa: E402


def devices_dir(explicit: str | None) -> Path:
    return Path(explicit) if explicit else REPO_ROOT / "devices"


def _get(devices: Path, name: str):
    try:
        return device_folder.load(device_folder.folder(devices, name))
    except device_folder.DeviceError as exc:
        raise SystemExit(str(exc))


def _firmware_arg(args) -> Path:
    firmware = Path(args.firmware)
    if not firmware.is_file():
        raise SystemExit(f"no firmware at {firmware}")
    return firmware


def cmd_create(args) -> int:
    devices = devices_dir(args.dir)
    try:
        device = device_folder.create(
            devices, args.name, _firmware_arg(args), board=args.board,
            mac=args.mac, psram_mb=args.psram, psram_octal=args.psram_octal,
            overlays=tuple(args.overlay))
    except device_folder.DeviceError as exc:
        raise SystemExit(str(exc))
    print(f"created {device.root}")
    print(f"  board    : {device.board}")
    print(f"  firmware : {device.firmware}")
    print(f"  mac      : {device.mac}")
    print(f"  card     : {'yes' if device.has_sdcard else 'no'}")
    print(f"start it  : device.py start {device.name}")
    return 0


def cmd_list(args) -> int:
    devices = devices_dir(args.dir)
    found = device_folder.all_devices(devices)
    if not found:
        print(f"no devices in {devices}")
        return 0
    for device in found:
        card = "card" if device.has_sdcard else "no card"
        print(f"{device.name:16s} {device.board:10s} {card:8s} "
              f"{device.firmware}")
    return 0


def cmd_show(args) -> int:
    device = _get(devices_dir(args.dir), args.name)
    for field in ("name", "board", "firmware", "firmware_sha256", "mac",
                  "psram_mb", "psram_octal", "created"):
        print(f"{field:16s} {getattr(device, field)}")
    if device.overlays:
        print(f"{'overlays':16s} {', '.join(device.overlays)}")
    print(f"{'storage':16s} {device.flash} + {device.efuse}"
          + (f" + {device.sdcard}" if device.has_sdcard else ""))
    return 0


def cmd_flash(args) -> int:
    device = _get(devices_dir(args.dir), args.name)
    try:
        device = device_folder.reflash(device, _firmware_arg(args),
                                       overlays=tuple(args.overlay))
    except device_folder.DeviceError as exc:
        raise SystemExit(str(exc))
    print(f"flashed {device.root}")
    print(f"  firmware : {device.firmware}")
    return 0


def cmd_remove(args) -> int:
    device = _get(devices_dir(args.dir), args.name)
    if not args.yes:
        answer = input(f"remove {device.root} and everything in it? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("kept")
            return 1
    device_folder.remove(device)
    print(f"removed {device.root}")
    return 0


def cmd_start(args) -> int:
    device = _get(devices_dir(args.dir), args.name)
    cmd = [sys.executable, str(REPO_ROOT / "emu" / "run.py"),
           "--device", str(device.root)] + args.run_args
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=None, metavar="DIR",
                    help="where device folders live (default: devices/ in the "
                         "repository)")
    sub = ap.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="make a device folder and assemble "
                                            "its storage once")
    create.add_argument("name")
    create.add_argument("--firmware", required=True)
    create.add_argument("--board", default="tdeck-max",
                        choices=("tdeck-max", "tdeck"))
    create.add_argument("--mac", default="02:54:44:4D:00:01")
    create.add_argument("--psram", type=int, default=8)
    create.add_argument("--psram-octal", action="store_true",
                        help="always present PSRAM as octal; without it each "
                             "boot takes the line mode of the image's own "
                             "PSRAM driver (ADR 0144)")
    create.add_argument("--overlay", action="append", default=[],
                        metavar="OFFSET=FILE")
    create.set_defaults(fn=cmd_create)

    sub.add_parser("list", help="the devices in the folder").set_defaults(fn=cmd_list)

    show = sub.add_parser("show", help="one device, in full")
    show.add_argument("name")
    show.set_defaults(fn=cmd_show)

    flash = sub.add_parser("flash", help="re-assemble an existing device's "
                                         "storage from a new image")
    flash.add_argument("name")
    flash.add_argument("--firmware", required=True)
    flash.add_argument("--overlay", action="append", default=[],
                       metavar="OFFSET=FILE")
    flash.set_defaults(fn=cmd_flash)

    remove = sub.add_parser("remove", help="delete a device and its storage")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true",
                        help="without a prompt, for scripts")
    remove.set_defaults(fn=cmd_remove)

    start = sub.add_parser("start", help="run the device (emu/run.py --device)")
    start.add_argument("name")
    start.add_argument("run_args", nargs=argparse.REMAINDER,
                       help="arguments for emu/run.py: --bus, --air, timeouts...")
    start.set_defaults(fn=cmd_start)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
