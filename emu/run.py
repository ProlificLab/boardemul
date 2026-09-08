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
import logging
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import mkflash  # noqa: E402
from emu import board  # noqa: E402
from emu import board_tdeck  # noqa: E402
from emu.devices import lora  # noqa: E402
from emu.bus import BusServer, IrqChannel  # noqa: E402

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
    stderr_log_path: str
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


def build_images(firmware: Path, outdir: Path, mac: str, overlays: list[str] = ()) -> tuple[Path, Path]:
    argv = [str(firmware), "-o", str(outdir), "--mac", mac]
    for spec in overlays:
        argv += ["--overlay", spec]
    rc = mkflash.main(argv)
    if rc != 0:
        raise SystemExit("mkflash refused the image; not launching QEMU")
    return outdir / "flash.bin", outdir / "efuse.bin"


def build_argv(qemu: Path, flash: Path, efuse: Path, uart: Path,
               qemu_log: Path, traced: bool, psram_mb: int,
               psram_octal: bool = False) -> list[str]:
    debug = ["unimp", "guest_errors"]
    # Escape hatch for one-off investigations (in_asm, exec, int...). Not a
    # supported interface; the supported one is --trace.
    extra = os.environ.get("BOARDEMUL_QEMU_DFLAGS")
    if extra:
        debug += [f.strip() for f in extra.split(",") if f.strip()]
    # Sibling hatch for arguments the debug list cannot express, -dfilter above
    # all: an unfiltered in_asm trace of a boot is hundreds of megabytes.
    passthrough = os.environ.get("BOARDEMUL_QEMU_ARGS", "").split()
    if traced:
        debug += [f"trace:{e}" for e in TRACE_EVENTS]
    return [
        str(qemu),
        "-machine", "esp32s3",
        "-display", "none",
        "-no-reboot",
        "-drive", f"file={flash},if=mtd,format=raw",
        "-m", f"{psram_mb}M",
        "-global",
        f"driver=ssi_psram,property=is_octal,value={'true' if psram_octal else 'false'}",
        "-drive", f"file={efuse},if=none,format=raw,id=efuse",
        "-global", "driver=nvram.esp32s3.efuse,property=drive,value=efuse",
        "-global", f"driver=esp32s3.gpio,property=strap_mode,value=0x{STRAP_MODE:02x}",
        "-serial", f"file:{uart}",
        "-rtc", f"base={RTC_BASE},clock=vm",
        "-seed", str(RNG_SEED),
        "-d", ",".join(debug),
        "-D", str(qemu_log),
    ] + passthrough


def run_qemu(argv: list[str], uart: Path, stderr_log: Path, timeout: float,
             stall: float, server: "BusServer | None" = None) -> tuple[str, float]:
    """Returns (exit_reason, wall_seconds).

    The guest will not exit on its own once it starts polling a register that
    never changes, so both a stall watchdog and a hard timeout are required.

    Liveness is UART output *or* bus traffic. Firmware that draws to a screen
    can be perfectly healthy while saying nothing at all on the serial port, and
    judging it by UART alone cuts the run off mid-picture.
    """
    uart.write_bytes(b"")
    started = time.monotonic()
    # QEMU's own reports (error_report, a debug build's info_report) go to
    # stderr. A pipe nobody drains fills at 64 KB and then blocks QEMU on the
    # next write, which is indistinguishable from a guest stall; a file never
    # blocks and keeps the text, which is the point of having it.
    errfile = open(stderr_log, "wb")
    previous = signal.signal(signal.SIGTERM, _take_the_guest_with_us)
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=errfile)

    last_size, last_bus = 0, 0
    last_change = time.monotonic()
    try:
        while True:
            if proc.poll() is not None:
                # The code says who ended it: a guest reset under -no-reboot
                # exits 0, an abort inside QEMU is nonzero, and a negative
                # code is a signal from outside, which no guest can cause.
                return f"guest exited (code {proc.returncode})", time.monotonic() - started
            now = time.monotonic()
            if now - started > timeout:
                return "hard timeout", now - started
            size = uart.stat().st_size if uart.exists() else 0
            bus_n = server.accesses if server else 0
            if size != last_size or bus_n != last_bus:
                last_size, last_bus, last_change = size, bus_n, now
            elif size > 0 and now - last_change > stall:
                return f"stalled ({stall:.0f}s idle)", now - started
            time.sleep(0.25)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        signal.signal(signal.SIGTERM, previous)
        errfile.close()


def _take_the_guest_with_us(signum, frame):
    """Turn a termination signal into an exception, so the teardown below runs.

    Python ends the process outright on SIGTERM without unwinding, which leaves
    QEMU orphaned and eating a core for as long as the machine is up. That
    matters to anything that stops a run early rather than waiting for it, and
    to anyone who kills one by hand.
    """
    raise KeyboardInterrupt(f"terminated by signal {signum}")


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
    # The board really has octal PSRAM, but the fork's SPI1 model does not
    # clock octal DDR the way IDF 4.4.6 drives it, so detection reads back all
    # zeros. Presented as quad, the same firmware enables PSRAM and runs.
    ap.add_argument("--psram-octal", action="store_true",
                    help="present PSRAM in octal line mode (matches the real "
                         "board, but detection fails under this QEMU fork)")
    ap.add_argument("--mac", default=mkflash.DEFAULT_MAC)
    ap.add_argument("--overlay", action="append", default=[], metavar="OFFSET=FILE",
                    help="splice a vendor filesystem image into the flash (ADR 0021)")
    ap.add_argument("--board", default="tdeck-max", choices=("tdeck-max", "tdeck"),
                    help="which board to wire up (default: tdeck-max)")
    ap.add_argument("--qemu", default=None)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every bus transaction at DEBUG")
    ap.add_argument("--bus", action="store_true",
                    help="intercept unmodelled peripherals with Python models")
    ap.add_argument("--air", default=None, metavar="DIR",
                    help="share a directory with other running boards so their "
                         "radios can hear each other (ADR 0036)")
    ap.add_argument("--at", default=None, metavar="X,Y",
                    help="where this board stands, in metres, so the radio "
                         "link to another board can be worked out (ADR 0038)")
    ap.add_argument("--antenna", type=float, default=0.0, metavar="DBI",
                    help="what this board's aerial adds, in dBi (default 0, "
                         "a plain one)")
    ap.add_argument("--environment", default="free-space",
                    help="the kind of place between the boards: "
                         + ", ".join(lora.ENVIRONMENTS) + ", or an exponent")
    ap.add_argument("--obstruction", type=float, default=0.0, metavar="DB",
                    help="anything else in the way, in dB, stated rather than "
                         "drawn at random so a run repeats (ADR 0039)")
    ap.add_argument("--icount", action="store_true",
                    help="drive the guest clock from executed instructions (ADR 0027): "
                         "exact timer delivery and deterministic runs; the Bluetooth "
                         "link layer needs it")
    ap.add_argument("--tag", default=None,
                    help="name for this run's logs (default: firmware stem)")
    args = ap.parse_args(argv)

    tag = args.tag or args.firmware.stem
    if args.trace:
        tag += "-traced"
    args.outdir.mkdir(parents=True, exist_ok=True)

    qemu = find_qemu(args.qemu)
    flash, efuse = build_images(args.firmware, args.builddir / args.firmware.stem, args.mac, args.overlay)

    uart = args.outdir / f"{tag}.uart.log"
    qlog = args.outdir / f"{tag}.qemu.log"
    elog = args.outdir / f"{tag}.stderr.log"
    cmd = build_argv(qemu, flash, efuse, uart, qlog, args.trace, args.psram,
                     psram_octal=args.psram_octal)

    hw_module = {"tdeck-max": board, "tdeck": board_tdeck}[args.board]
    # SoC-level, so it applies with or without --bus: these describe the chip
    # the board is built around, not the peripherals we model in Python.
    for spec in hw_module.SOC_DEVICES:
        cmd += ["-device", spec]
    # One translation thread, always. Two of them race inside QEMU under load
    # and take the whole emulator down with a segmentation fault -- rare, never
    # reproducible on an idle machine, and it cost a test run to catch. It is
    # also no slower here and sometimes much faster, because the peripherals
    # this board models are serialised through one bus anyway (ADR 0041).
    cmd += ["-accel", "tcg,thread=single"]
    if args.icount:
        # 2^2 ns per instruction, 250 MIPS nominal; the vCPU sleeps when the
        # guest idles. Instruction counting requires the single thread above.
        cmd += ["-icount", "shift=2,sleep=on"]

    server: BusServer | None = None
    tail = None
    irq: IrqChannel | None = None
    hw: board.Board | None = None
    if args.bus:
        logging.basicConfig(filename=args.outdir / f"{tag}.bus.log", filemode="w",
                            level=logging.DEBUG if args.verbose else logging.INFO,
                            format="%(name)s %(message)s")
        where = (0.0, 0.0)
        if args.at:
            east, _, north = args.at.partition(",")
            where = (float(east), float(north or 0))
        try:
            exponent = lora.ENVIRONMENTS.get(args.environment)
            if exponent is None:
                exponent = float(args.environment)
        except ValueError:
            raise SystemExit(f"unknown environment {args.environment!r}; "
                             f"use one of {', '.join(lora.ENVIRONMENTS)} "
                             f"or an exponent")
        try:
            hw = hw_module.build(args.outdir, air=args.air, position=where,
                                 antenna_dbi=args.antenna, exponent=exponent,
                                 obstruction_db=args.obstruction)
        except TypeError:
            # Boards that have no radio take none of this.
            hw = hw_module.build(args.outdir)
        hw.start()
        server = BusServer(args.outdir / f"{tag}.bus.sock", hw.regions)
        irq = IrqChannel(args.outdir / f"{tag}.irq.sock")
        irq.start()
        server.irq_channel = irq
        tail = hw_module.attach_input(server, hw, args.outdir)
        server.start()
        cmd += server.qemu_args(irq.socket_path) + hw.qemu_args()

    print(f"\nqemu    : {qemu}")
    print(f"trace   : {'on (slow)' if args.trace else 'off'}")
    print(f"bus     : {'on' if server else 'off'}")
    if hw_module.SOC_DEVICES:
        print(f"soc     : {', '.join(hw_module.SOC_DEVICES)}")
    print(f"uart    : {uart}")
    try:
        reason, wall = run_qemu(cmd, uart, elog, args.timeout, args.stall, server)
    finally:
        if server:
            _report_bus(server, tail, irq, hw)
            server.stop()
        if tail:
            tail.stop()
        if irq:
            irq.stop()
        if hw:
            hw.stop()

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
        stderr_log_path=str(elog),
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


def _report_bus(server: BusServer, tail=None, irq=None, hw=None) -> None:
    from emu.devices.i2c_devices import CST328, TCA8418
    from emu.devices.tdeck_i2c import GT911, TDeckKeyboard

    if hw is not None:
        hw.panel.flush()
        print(f"  spi2: {hw.spi.segments} segment(s), {hw.spi.bytes_in} byte(s) in"
              + (f"  ({hw.spi.bytes_in / hw.spi.segments:.0f} bytes/segment)"
                 if hw.spi.segments else ""))
        print(f"    {hw.panel.summary()}")
        latest = getattr(hw.panel, "latest", None)
        if latest:
            print(f"    latest frame: {latest}")
        for bus in (hw.i2c0, hw.i2c1):
            if not bus.transactions:
                continue
            print(f"  {bus.name}: {bus.transactions} transaction(s)")
            acked = " ".join(f"0x{a:02x}" for a in sorted(bus.acks))
            print(f"    acked: {acked or 'none'}")
            # What the firmware looked for and did not find is how a board's
            # device list gets discovered in the first place (ADR 0006).
            missing = " ".join(f"0x{a:02x}" for a in sorted(bus.nacks))
            if missing:
                print(f"    probed, no model: {missing}")
            for dev in bus.devices.values():
                if isinstance(dev, CST328) and dev.reports:
                    print(f"    CST328: {dev.reports} report(s) read, "
                          f"{dev.acks} acknowledged")
                elif isinstance(dev, TCA8418) and dev.pending:
                    print(f"    TCA8418: {len(dev.pending)} event(s) still queued")
                elif isinstance(dev, GT911):
                    print(f"    GT911: {dev.reports} report(s) read, "
                          f"{dev.acks} acknowledged")
                elif isinstance(dev, TDeckKeyboard):
                    print(f"    keyboard: polled {dev.reads} time(s), "
                          f"{dev.delivered} character(s) delivered, "
                          f"{len(dev.pending)} still queued")
        print(f"  gpio: {hw.gpio.sent} pin level(s) driven from the host")

    busy = sorted((r for r in server.regions if r.accesses),
                  key=lambda r: -r.accesses)
    if busy:
        print("  register-level bridge traffic:")
        for r in busy[:6]:
            print(f"    {r.name:8} {r.accesses:9,}")

    if server.recent:
        print("  last accesses (the tail is what it is stuck on):")
        for op, addr, size, value in list(server.recent)[-12:]:
            kind = "rd" if op == 0 else "wr"
            print(f"    {kind} 0x{addr:08x} size {size} value 0x{value:08x}")
    print("-" * 60)


if __name__ == "__main__":
    sys.exit(main())
