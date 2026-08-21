#!/usr/bin/env python3
"""Boot a vendor firmware image under QEMU and capture what it does.

See docs/adr/0004-running-and-observing.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import mkflash  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Pinned so that a rerun is a rerun. See ADR 0004 decision 4.
RNG_SEED = 1
RTC_BASE = "2020-01-01"
STRAP_MODE = 0x04  # SPI flash boot

TRACE_EVENTS = ("memory_region_ops_read", "memory_region_ops_write")

# How long the UART may stay silent before we call it stalled. The guest polls
# a status bit that never sets, so it will never terminate on its own.
DEFAULT_STALL_SECONDS = 8.0
DEFAULT_TIMEOUT_SECONDS = 45.0


@dataclass
class RunResult:
    qemu_version: str
    argv: list[str]
    inputs: dict[str, str]
    exit_reason: str
    wall_seconds: float
    uart_bytes: int
    uart_path: str
    qemu_log_path: str
    traced: bool
    seed: int = RNG_SEED
    rtc_base: str = RTC_BASE
    strap_mode: int = STRAP_MODE
    notes: list[str] = field(default_factory=list)


def find_qemu(explicit: str | None = None) -> Path:
    """ADR 0002 decision 5: nothing here may assume the devcontainer. An
    override, then the local build, then whatever is on PATH."""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"--qemu {p} does not exist")
        return p
    env = os.environ.get("BOARDEMUL_QEMU")
    if env and Path(env).is_file():
        return Path(env)
    local = REPO_ROOT / "third_party" / "qemu" / "build" / "qemu-system-xtensa"
    if local.is_file():
        return local
    found = shutil.which("qemu-system-xtensa")
    if found:
        return Path(found)
    raise FileNotFoundError(
        "no qemu-system-xtensa found. Run tools/build_qemu.sh, or set "
        "BOARDEMUL_QEMU to a built binary."
    )


def qemu_version(qemu: Path) -> str:
    out = subprocess.run([str(qemu), "--version"], capture_output=True, text=True)
    return out.stdout.splitlines()[0].strip() if out.stdout else "unknown"


def build_images(firmware: Path, outdir: Path, mac: str) -> tuple[Path, Path]:
    rc = mkflash.main([str(firmware), "-o", str(outdir), "--mac", mac])
    if rc != 0:
        raise SystemExit("mkflash refused the image; not launching QEMU")
    return outdir / "flash.bin", outdir / "efuse.bin"


def build_argv(qemu: Path, flash: Path, efuse: Path, uart: Path,
               qemu_log: Path, traced: bool, psram_mb: int) -> list[str]:
    debug = ["unimp", "guest_errors"]
    if traced:
        debug += [f"trace:{e}" for e in TRACE_EVENTS]
    return [
        str(qemu),
        "-machine", "esp32s3",
        "-display", "none",
        "-no-reboot",
        "-drive", f"file={flash},if=mtd,format=raw",
        "-m", f"{psram_mb}M",
        "-global", "driver=ssi_psram,property=is_octal,value=true",
        "-drive", f"file={efuse},if=none,format=raw,id=efuse",
        "-global", "driver=nvram.esp32s3.efuse,property=drive,value=efuse",
        "-global", f"driver=esp32s3.gpio,property=strap_mode,value=0x{STRAP_MODE:02x}",
        "-serial", f"file:{uart}",
        "-rtc", f"base={RTC_BASE},clock=vm",
        "-seed", str(RNG_SEED),
        "-d", ",".join(debug),
        "-D", str(qemu_log),
    ]


def run_qemu(argv: list[str], uart: Path, timeout: float,
             stall: float) -> tuple[str, float]:
    """Returns (exit_reason, wall_seconds).

    The guest will not exit on its own once it starts polling a register that
    never changes, so both a stall watchdog and a hard timeout are required.
    """
    uart.write_bytes(b"")
    started = time.monotonic()
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    last_size, last_change = 0, time.monotonic()
    try:
        while True:
            if proc.poll() is not None:
                return "guest exited", time.monotonic() - started
            now = time.monotonic()
            if now - started > timeout:
                return "hard timeout", now - started
            size = uart.stat().st_size if uart.exists() else 0
            if size != last_size:
                last_size, last_change = size, now
            elif size > 0 and now - last_change > stall:
                return f"stalled ({stall:.0f}s of UART silence)", now - started
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware", type=Path)
    ap.add_argument("--outdir", type=Path, default=REPO_ROOT / "logs")
    ap.add_argument("--builddir", type=Path, default=REPO_ROOT / "build")
    ap.add_argument("--trace", action="store_true",
                    help="record every MMIO access (roughly 100x slower)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    ap.add_argument("--stall", type=float, default=DEFAULT_STALL_SECONDS)
    ap.add_argument("--psram", type=int, default=8, help="PSRAM size in MB")
    ap.add_argument("--mac", default=mkflash.DEFAULT_MAC)
    ap.add_argument("--qemu", default=None)
    ap.add_argument("--tag", default=None,
                    help="name for this run's logs (default: firmware stem)")
    args = ap.parse_args(argv)

    tag = args.tag or args.firmware.stem
    if args.trace:
        tag += "-traced"
    args.outdir.mkdir(parents=True, exist_ok=True)

    qemu = find_qemu(args.qemu)
    flash, efuse = build_images(args.firmware, args.builddir / args.firmware.stem, args.mac)

    uart = args.outdir / f"{tag}.uart.log"
    qlog = args.outdir / f"{tag}.qemu.log"
    cmd = build_argv(qemu, flash, efuse, uart, qlog, args.trace, args.psram)

    print(f"\nqemu    : {qemu}")
    print(f"trace   : {'on (slow)' if args.trace else 'off'}")
    print(f"uart    : {uart}")
    reason, wall = run_qemu(cmd, uart, args.timeout, args.stall)

    result = RunResult(
        qemu_version=qemu_version(qemu),
        argv=cmd,
        inputs={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (args.firmware, flash, efuse)},
        exit_reason=reason,
        wall_seconds=round(wall, 2),
        uart_bytes=uart.stat().st_size if uart.exists() else 0,
        uart_path=str(uart),
        qemu_log_path=str(qlog),
        traced=args.trace,
    )
    if args.trace:
        result.notes.append("timing is meaningless in a traced run")
    manifest = args.outdir / f"{tag}.manifest.json"
    manifest.write_text(json.dumps(asdict(result), indent=2))

    print(f"result  : {reason} after {wall:.1f}s, {result.uart_bytes} bytes of UART")
    print(f"manifest: {manifest}\n")
    if uart.exists() and result.uart_bytes:
        print("--- last UART lines " + "-" * 40)
        tail = uart.read_text(errors="replace").splitlines()[-12:]
        for line in tail:
            print("  " + line)
        print("-" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
