#!/usr/bin/env python3
"""Boot a vendor firmware image under QEMU and capture what it does.

See docs/adr/0004-running-and-observing.md.
"""

from __future__ import annotations

import argparse
import faulthandler
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
from emu import device as device_folder  # noqa: E402
from tools import mkflash  # noqa: E402
from tools.fetch_firmware import companion_overlays as firmware_companions  # noqa: E402
from emu import board  # noqa: E402
from emu import board_tdeck  # noqa: E402
from emu.devices import lora  # noqa: E402
from emu.devices.air import HOST as AIR_LOOPBACK, air_host  # noqa: E402
from emu.serial_input import SerialBridge, SerialInput  # noqa: E402
from emu.bus import BusServer, IrqChannel  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Pinned so that a rerun is a rerun. See ADR 0004 decision 4.
RNG_SEED = 1


def rng_seed(mac: str) -> int:
    """The hardware RNG's seed for one board: the fixed base in the high bits
    and the board's MAC in the low 48. Fixed per board, so a run is still
    determined by its inputs (ADR 0004); different between boards, as two real
    chips' random streams are -- a shared one had two mesh nodes pick the same
    'random' node number (ADR 0098)."""
    return (RNG_SEED << 48) | int(mac.replace(":", "").replace("-", ""), 16)
RTC_BASE = "2020-01-01"
STRAP_MODE = 0x04  # SPI flash boot

TRACE_EVENTS = ("memory_region_ops_read", "memory_region_ops_write")

# How long one core runs before the other gets the host, in microseconds of
# virtual time (ADR 0161); BOARDEMUL_CORE_SLICE_US overrides it, 0 turns the
# shorter turn off and leaves QEMU's 100 ms.
CORE_SLICE_US = 1000

# How long the UART may stay silent before we call it stalled. The guest polls
# a status bit that never sets, so it will never terminate on its own.
DEFAULT_STALL_SECONDS = 8.0
DEFAULT_TIMEOUT_SECONDS = 45.0
# Teardown's patience (ADR 0136): past this, every thread's stacks are
# dumped into the run's directory and the process exits. It has to beat the
# suite's own teardown kill (15 s) with room for the QEMU shutdown (5 s)
# that runs before the arming, hence 8.
TEARDOWN_WATCHDOG_SECONDS = 8.0


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
    # The chip's USB serial port, as the host on the other end received it
    # (ADR 0100). A firmware whose Serial is that port says nothing on the UART.
    usb_bytes: int = 0
    usb_path: str = ""
    # The device folder a run started from, when it started from one (ADR 0133).
    device: str | None = None
    rtc_base: str = RTC_BASE
    strap_mode: int = STRAP_MODE
    notes: list[str] = field(default_factory=list)
    # What the radio did, when the board has one. A mesh is two boards
    # agreeing about this, so it has to be readable after the fact
    # rather than only visible in a debug log.
    radio: dict = field(default_factory=dict)
    # What the screen's backlight did. A firmware that dims or blanks the panel
    # says so here rather than only in a log line, so a run can be asked about
    # it afterwards (ADR 0053).
    backlight: dict = field(default_factory=dict)
    # Every window this board claims on the register bridge, and how much the
    # firmware used it. This is the list the next piece of hardware to model
    # comes off (ADR 0053).
    unmodelled: dict = field(default_factory=dict)
    # What the host on the USB OTG cable saw (ADR 0166).
    usb_otg: str = ""
    usb_otg_bytes: int = 0


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


# The log tags of ESP-IDF's two ESP32-S3 PSRAM drivers, which a build links
# one of (ADR 0144). IDF 4.4 tags them "opi psram" and "psram", IDF 5
# "octal_psram" and "quad_psram". "psram" counts only as a whole string, so
# that the octal driver's own tag is not read as the quad one.
OCTAL_PSRAM_TAGS = (b"opi psram\x00", b"octal_psram\x00")
QUAD_PSRAM_TAGS = (b"\x00psram\x00", b"quad_psram\x00")


def image_psram_mode(image: bytes) -> str | None:
    """"octal" or "quad" if the image carries that PSRAM driver alone, else
    None: no driver's tag, or both (ADR 0144)."""
    octal = any(tag in image for tag in OCTAL_PSRAM_TAGS)
    quad = any(tag in image for tag in QUAD_PSRAM_TAGS)
    if octal == quad:
        return None
    return "octal" if octal else "quad"


def choose_psram_mode(forced: str | None, image: Path | None) -> tuple[bool, str]:
    """Whether to present PSRAM as octal, and why: what a person forced, else
    the image's own driver, else ADR 0007's quad."""
    if forced:
        return forced == "octal", f"{forced}, as asked"
    found = image_psram_mode(image.read_bytes()) if image and image.is_file() else None
    if found:
        return found == "octal", f"{found}, the image's driver"
    return False, "quad, the default: the image names no single PSRAM driver"


def build_argv(qemu: Path, flash: Path, efuse: Path, uart: Path,
               qemu_log: Path, traced: bool, psram_mb: int,
               psram_octal: bool = False, seed: int = RNG_SEED,
               reboot: bool = False, usb: Path | None = None,
               serial_in: dict[str, Path] | None = None) -> list[str]:
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
    # The chip's USB serial port, with a host listening on it (ADR 0100). It is
    # silicon, so it is there on every board; what the host hears goes to a file
    # beside the UART's when the run names one.
    # Each port reads a pipe a person can type into (ADR 0156).
    serial_in = serial_in or {}
    usb_in = f",input-path={serial_in['usb']}" if "usb" in serial_in else ""
    usb_port = (["-chardev", f"file,id=usbjtag,path={usb}{usb_in}",
                 "-device", "esp-usb-serial-jtag,chardev=usbjtag"]
                if usb is not None else ["-device", "esp-usb-serial-jtag"])
    uart_port = (["-chardev", f"file,id=uart0,path={uart},input-path={serial_in['uart']}",
                  "-serial", "chardev:uart0"]
                 if "uart" in serial_in else ["-serial", f"file:{uart}"])
    return [
        str(qemu),
        "-machine", "esp32s3",
        "-display", "none",
    ] + ([] if reboot else ["-no-reboot"]) + [
        "-drive", f"file={flash},if=mtd,format=raw",
        "-m", f"{psram_mb}M",
        "-global",
        f"driver=ssi_psram,property=is_octal,value={'true' if psram_octal else 'false'}",
        "-drive", f"file={efuse},if=none,format=raw,id=efuse",
        "-global", "driver=nvram.esp32s3.efuse,property=drive,value=efuse",
        "-global", f"driver=esp32s3.gpio,property=strap_mode,value=0x{STRAP_MODE:02x}",
        # A full RX FIFO loses a byte, as silicon does (ADR 0122). Upstream
        # took the fix behind this property, off by default; the base type's
        # global reaches the ESP32-S3's UARTs (ADR 0138).
        "-global", "driver=esp_soc.uart,property=rx-overflow,value=on",
    ] + uart_port + [
        "-rtc", f"base={RTC_BASE},clock=vm",
        "-seed", str(seed),
        "-d", ",".join(debug),
        "-D", str(qemu_log),
    ] + usb_port + passthrough


def run_qemu(argv: list[str], uart: Path, stderr_log: Path, timeout: float,
             stall: float, server: "BusServer | None" = None,
             usb: Path | None = None) -> tuple[str, float]:
    """Returns (exit_reason, wall_seconds).

    The guest will not exit on its own once it starts polling a register that
    never changes, so both a stall watchdog and a hard timeout are required.

    Liveness is serial output *or* bus traffic. Firmware that draws to a screen
    can be perfectly healthy while saying nothing at all on the serial port, and
    judging it by UART alone cuts the run off mid-picture. Serial output is the
    UART's and the USB port's together: a firmware whose Serial is the USB port
    says nothing on the UART after the ROM's lines (ADR 0100).
    """
    uart.write_bytes(b"")
    if usb is not None:
        usb.write_bytes(b"")
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
            size = sum(p.stat().st_size for p in (uart, usb)
                       if p is not None and p.exists())
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


def arm_teardown_watchdog(outdir: Path) -> None:
    """Bounds the shutdown: past the watchdog, every thread's stacks land in
    the run's directory and the process exits (ADR 0136)."""
    outdir = Path(outdir)
    if TEARDOWN_WATCHDOG_SECONDS <= 0:
        return
    log = (outdir / "teardown.log").open("wb")
    faulthandler.dump_traceback_later(TEARDOWN_WATCHDOG_SECONDS, exit=True,
                                      file=log)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware", type=Path, nargs="?", default=None)
    ap.add_argument("--device", type=Path, default=None, metavar="PATH",
                    help="start a device folder instead of a bare firmware: "
                         "its storage is reused and kept (ADR 0133)")
    ap.add_argument("--outdir", type=Path, default=REPO_ROOT / "logs")
    ap.add_argument("--builddir", type=Path, default=REPO_ROOT / "build")
    ap.add_argument("--trace", action="store_true",
                    help="record every MMIO access (roughly 100x slower)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    ap.add_argument("--stall", type=float, default=DEFAULT_STALL_SECONDS)
    ap.add_argument("--psram", type=int, default=None, help="PSRAM size in MB")
    # Each image enumerates its PSRAM only in the line mode of the driver it
    # was built with, so by default the mode is read off the image (ADRs
    # 0007, 0144). These force one.
    psram_mode = ap.add_mutually_exclusive_group()
    psram_mode.add_argument("--psram-octal", action="store_true",
                            help="present PSRAM in octal line mode, whatever "
                                 "driver the image carries")
    psram_mode.add_argument("--psram-quad", action="store_true",
                            help="present PSRAM in quad line mode, whatever "
                                 "driver the image carries")
    ap.add_argument("--mac", default=None)
    ap.add_argument("--overlay", action="append", default=[], metavar="OFFSET=FILE",
                    help="splice a vendor filesystem image into the flash (ADR "
                         "0021). With --device the storage is already built "
                         "and this is refused")
    ap.add_argument("--board", default=None, choices=("tdeck-max", "tdeck"),
                    help="which board to wire up (default: tdeck-max, or the "
                         "device's)")
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
    ap.add_argument("--gps", default=None, metavar="LAT,LON[,ALT]",
                    help="put a satellite receiver on the board's second UART "
                         "and have it report this position, in degrees and "
                         "metres (ADR 0052). Without it the board has no "
                         "module, which is how they are sold")
    ap.add_argument("--wifi-ap", default=None, metavar="SSID[,MS[,CHANNEL]]",
                    help="put an access point in the air: its name, how often "
                         "it beacons in milliseconds (100 by default), and the "
                         "one channel it is on (1 by default, ADR 0110)")
    ap.add_argument("--wifi-wpa2", action="store_true",
                    help="the access point has a passphrase: it advertises "
                         "WPA2-PSK, which is what a station that was given "
                         "one looks for")
    ap.add_argument("--wifi-pass", default=None, metavar="PASSPHRASE",
                    help="the passphrase of a WPA2 network, which the four-way "
                         "handshake derives its keys from together with the "
                         "name; required with --wifi-wpa2")
    ap.add_argument("--wifi-uplink", action="store_true",
                    help="route what the station sends beyond the LAN to the "
                         "real internet through this host's sockets, and look "
                         "names up through its resolver (ADR 0097). Off by "
                         "default: guest traffic then never leaves the machine")
    ap.add_argument("--wifi-forward", action="append", default=[],
                    metavar="HOSTPORT:DEVICEPORT",
                    help="listen on HOSTPORT of this machine's loopback and "
                         "connect each client to DEVICEPORT of the station on "
                         "the access point, e.g. 4403:4403 for Meshtastic's "
                         "TCP API (ADR 0164). May be given more than once")
    ap.add_argument("--ble-air", default=None, metavar="DIR",
                    help="share a directory so this board's Bluetooth can be "
                         "heard by others; a different band from --air, so a "
                         "different directory (ADR 0045)")
    ap.add_argument("--air-boards", type=int, default=0, metavar="N",
                    help="the boards on --air keep one time: N of them, this one "
                         "included, advancing together a quantum at a time "
                         "(ADR 0113). Needs --icount")
    ap.add_argument("--air-quantum-us", type=int, default=1000, metavar="US",
                    help="how far a timed air's boards run between meeting, in "
                         "microseconds of guest time (default 1000)")
    ap.add_argument("--audio", action="store_true",
                    help="write what the speaker plays to WAV files in the "
                         "run's audio/ directory, one per stream (ADR 0146)")
    ap.add_argument("--serial-tcp", default=None, metavar="PORT[:uart|usb|otg]",
                    help="put a serial port on this TCP port of the host's "
                         "loopback, the USB Serial/JTAG port unless one is "
                         "named, for the host's own tools: a terminal, a "
                         "script (ADRs 0158, 0166)")
    ap.add_argument("--mic", default=None, metavar="FILE.wav",
                    help="the microphone hears this PCM WAV file, from its "
                         "start at each capture (ADR 0146)")
    ap.add_argument("--icount", action="store_true",
                    help="drive the guest clock from executed instructions (ADR "
                         "0027); the default since ADR 0178, still accepted")
    ap.add_argument("--host-clock", action="store_true",
                    help="drive the guest clock from the host's instead: faster, "
                         "less faithful -- firmware deadlines can then be missed "
                         "when the host is busy (ADRs 0141, 0178)")
    ap.add_argument("--reboot", action="store_true",
                    help="let a guest reset restart the chip, as the board does "
                         "after a panic, instead of ending the run (ADR 0099)")
    ap.add_argument("--tag", default=None,
                    help="name for this run's logs (default: firmware stem)")
    args = ap.parse_args(argv)
    # Instructions count the guest's time unless the host's clock is asked
    # for (ADR 0178).
    if args.icount and args.host_clock:
        raise SystemExit("--icount and --host-clock name two different clocks; "
                         "give one")
    args.icount = not args.host_clock

    # --device or a bare firmware, never both, never neither. The device is
    # identity: its storage, board and radio personality are the folder's,
    # and a flag only overrides where it also says something for this run.
    if (args.device is None) == (args.firmware is None):
        raise SystemExit("give either a firmware image or --device PATH")
    if args.device is not None:
        try:
            device = device_folder.load(args.device)
        except device_folder.DeviceError as exc:
            raise SystemExit(str(exc))
        # Until this run ends the folder is not a state the device was ever in:
        # a snapshot or a new card waits for it (ADR 0149).
        device_folder.hold(device)
        if args.overlay:
            raise SystemExit("--device keeps its own storage; --overlay would "
                             "rebuild it (use tools/device.py flash)")
        firmware = Path(device.firmware) if Path(device.firmware).is_file() else None
        board_name = args.board or device.board
        mac = args.mac or device.mac
        psram_mb = args.psram if args.psram is not None else device.psram_mb
        tag = args.tag or device.name
        flash, efuse = device.flash, device.efuse
    else:
        device = None
        firmware = args.firmware
        board_name = args.board or "tdeck-max"
        mac = args.mac or mkflash.DEFAULT_MAC
        psram_mb = args.psram if args.psram is not None else 8
        tag = args.tag or args.firmware.stem
    if args.trace:
        tag += "-traced"
    args.outdir.mkdir(parents=True, exist_ok=True)

    qemu = find_qemu(args.qemu)
    if device is None:
        overlays = list(args.overlay)
        if not overlays:
            # A pinned firmware brings what its installer writes beside it
            # (ADR 0150).
            overlays, missing = firmware_companions(args.firmware)
            for spec in overlays:
                print(f"overlay : {spec.replace('=', ' = ', 1)}, the firmware's own filesystem")
            for name in missing:
                print(f"overlay : {name} belongs with this firmware and is not "
                      f"fetched: python tools/fetch_firmware.py --only {name}")
        flash, efuse = build_images(args.firmware,
                                    args.builddir / args.firmware.stem,
                                    mac, overlays)
    # A device created with --psram-octal keeps forcing it; created without,
    # it takes the image's mode like a bare run (ADR 0144).
    forced = ("octal" if args.psram_octal else "quad" if args.psram_quad
              else "octal" if device is not None and device.psram_octal else None)
    psram_octal, psram_why = choose_psram_mode(forced, flash)

    uart = args.outdir / f"{tag}.uart.log"
    usb = args.outdir / f"{tag}.usb.log"
    qlog = args.outdir / f"{tag}.qemu.log"
    elog = args.outdir / f"{tag}.stderr.log"
    # What QEMU writes as the guest runs stays on local disk where the run's
    # folder is shared from the host, and is copied there as it grows: over
    # Docker Desktop's 9P every line was a round trip the guest waited on
    # (ADR 0174). On local disk these are the same paths.
    from emu.local_mirror import Mirror
    mirror = Mirror([uart, usb, qlog])
    seed = rng_seed(mac)
    # What a person types into the serial ports (ADR 0156).
    serial_input = SerialInput(args.outdir)
    bridge = None
    if args.serial_tcp:
        # Loopback, unless the run is in the build container, whose port is
        # published on the host's loopback only (ADR 0158).
        tcp_port, _, which = args.serial_tcp.partition(":")
        which = which or "usb"
        if which not in ("uart", "usb", "otg") or not tcp_port.isdigit():
            raise SystemExit("--serial-tcp is PORT, PORT:uart, PORT:usb or PORT:otg")
        bind = "0.0.0.0" if os.environ.get("BOARDEMUL_BIND_ALL") else "127.0.0.1"
        # The OTG port's log is the board's, written as the host reads it
        # (ADR 0166); where it grows is known once the board is built.
        logs = {"uart": mirror.local(uart), "usb": mirror.local(usb),
                "otg": args.outdir / f"{tag}.otg.log"}
        bridge = SerialBridge(which, int(tcp_port), logs[which], serial_input, bind=bind)
    cmd = build_argv(qemu, flash, efuse, mirror.local(uart), mirror.local(qlog),
                     args.trace, psram_mb, psram_octal=psram_octal, seed=seed,
                     reboot=args.reboot, usb=mirror.local(usb),
                     serial_in=serial_input.paths)

    hw_module = {"tdeck-max": board, "tdeck": board_tdeck}[board_name]
    # SoC-level, so it applies with or without --bus: these describe the chip
    # the board is built around, not the peripherals we model in Python.
    for spec in hw_module.SOC_DEVICES:
        cmd += ["-device", spec]
    # One translation thread, always. Two of them race inside QEMU under load
    # and take the whole emulator down with a segmentation fault -- rare, never
    # reproducible on an idle machine, and it cost a test run to catch. It is
    # also no slower here and sometimes much faster, because the peripherals
    # this board models are serialised through one bus anyway (ADR 0041).
    # BOARDEMUL_TCG_THREADS=multi puts the second thread back, which is the
    # configuration that crashes; it is here so the defect can be worked on
    # rather than only avoided.
    cmd += ["-accel", "tcg,thread=" + os.environ.get("BOARDEMUL_TCG_THREADS", "single")]
    # On that one thread the cores take turns; a core waiting on the other
    # spins out its whole turn, 100 ms by QEMU's default. This turn is shorter
    # (ADR 0161). It does nothing under -icount or on two threads.
    cmd += ["-device", "esp-core-slice,period_us="
            + os.environ.get("BOARDEMUL_CORE_SLICE_US", str(CORE_SLICE_US))]
    if args.icount:
        # 2^2 ns per instruction, 250 MIPS nominal; the vCPU sleeps when the
        # guest idles. Instruction counting requires the single thread above.
        # On a timed air it does not sleep: an idle guest's clock jumps to its
        # next deadline instead of following the host's, which is what QEMU
        # documents as deterministic -- and determinism is what a shared air
        # is for (ADR 0113).
        cmd += ["-icount", "shift=2,sleep=" + ("off" if args.air_boards else "on")]
        # On this clock the USB host's replies land at their cause's guest
        # instant, which a timed air's sameness needs; waiting for them costs
        # the guest no time here, and would without -icount (ADR 0166).
        cmd += ["-global", "esp-usb-otg.sync=on"]

    # Everything a board provides needs --bus, because without it no board is
    # built at all: no Python devices, no WiFi MAC, nothing on any bus. A run
    # like that still boots, and boots plausibly -- it just finds nothing
    # anywhere and spends a second of timeout per probe. Two investigation runs
    # were spent on that before it was noticed (ADR 0056), so it is refused.
    needs_board = {"--air": args.air, "--ble-air": args.ble_air,
                   "--gps": args.gps, "--at": args.at,
                   "--wifi-ap": args.wifi_ap, "--audio": args.audio,
                   "--mic": args.mic}
    given = [name for name, value in needs_board.items() if value]
    if given and not args.bus:
        raise SystemExit(", ".join(given) + " needs --bus: without it no board "
                         "is built, so nothing would answer")

    if args.air_boards:
        # A barrier stops the guest's time only when the vCPU runs the clock's
        # timer itself, which is what -icount does (ADR 0113). Without it the
        # guest would run on while its board waited.
        if not args.air:
            raise SystemExit("--air-boards says how many boards share --air; "
                             "give the directory too")
        if not args.icount:
            raise SystemExit("--air-boards needs --icount: only an "
                             "instruction-counted clock stops at a barrier")
        if args.air_quantum_us <= 0:
            raise SystemExit("--air-quantum-us must be positive")

    access_point: tuple | None = None
    if args.wifi_uplink and not args.wifi_ap:
        raise SystemExit("--wifi-uplink connects an access point to the "
                         "internet; --wifi-ap says there is one")
    if args.wifi_forward and not args.wifi_ap:
        raise SystemExit("--wifi-forward reaches the station on an access "
                         "point; --wifi-ap says there is one")
    forwards = []
    for spec in args.wifi_forward:
        host_port, _, device_port = spec.partition(":")
        if not (host_port.isdigit() and device_port.isdigit()
                and 1024 <= int(host_port) <= 65535
                and 1 <= int(device_port) <= 65535):
            raise SystemExit(f"--wifi-forward is HOSTPORT:DEVICEPORT, the host "
                             f"port from 1024, not {spec!r}")
        forwards.append((int(host_port), int(device_port)))
    if args.wifi_ap:
        parts = args.wifi_ap.split(",")
        if not parts[0]:
            raise SystemExit("--wifi-ap wants a name")
        if args.wifi_pass and not args.wifi_wpa2:
            raise SystemExit("--wifi-pass is the passphrase of a secured "
                             "network; --wifi-wpa2 says it is secured")
        if args.wifi_wpa2 and not args.wifi_pass:
            raise SystemExit("--wifi-wpa2 needs --wifi-pass: the four-way "
                             "handshake derives its keys from the passphrase "
                             "and the network name, and a station that cannot "
                             "complete it disconnects a second after "
                             "associating")
        access_point = (parts[0],
                        int(parts[1]) if len(parts) > 1 and parts[1] else 100,
                        int(parts[2]) if len(parts) > 2 and parts[2] else 1,
                        bool(args.wifi_wpa2), args.wifi_pass or "",
                        bool(args.wifi_uplink), tuple(forwards),
                        # Loopback, unless in the build container, whose
                        # ports are published on the host's loopback only.
                        "0.0.0.0" if os.environ.get("BOARDEMUL_BIND_ALL")
                        else "127.0.0.1")
    elif args.wifi_wpa2:
        raise SystemExit("--wifi-wpa2 says how the access point is secured; "
                         "--wifi-ap says there is one")
    server: BusServer | None = None
    tail = None
    irq: IrqChannel | None = None
    hw: board.Board | None = None
    # The digest of the device's card as loaded; None until it is, so a run
    # that failed to load it never writes the board's empty card over it.
    card_loaded: str | None = None
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
        fix = None
        if args.gps:
            parts = args.gps.split(",")
            if len(parts) not in (2, 3):
                raise SystemExit("--gps wants LAT,LON or LAT,LON,ALT in degrees "
                                 "and metres")
            fix = (float(parts[0]), float(parts[1]),
                   float(parts[2]) if len(parts) > 2 else 0.0)
        # Every board takes the same arguments, so this is a plain call. It used
        # to be wrapped in `except TypeError: build(outdir)`, and when one board
        # gained an argument the other did not, that quietly dropped it back to
        # a build with no air -- the radio options did nothing on it and nothing
        # said so (ADR 0052).
        if args.air or args.ble_air:
            # Resolved once, so every part of this board advertises the same
            # address (ADR 0145).
            os.environ["BOARDEMUL_AIR_HOST"] = air_host()
            if air_host() != AIR_LOOPBACK:
                print(f"air-host: {air_host()}")
        if args.ble_air:
            # The Bluetooth core writes its file there and does not make it.
            Path(args.ble_air).mkdir(parents=True, exist_ok=True)
        if args.mic and not Path(args.mic).is_file():
            raise SystemExit(f"--mic: no file at {args.mic}")
        hw = hw_module.build(args.outdir, air=args.air, ble_air=args.ble_air,
                             position=where, antenna_dbi=args.antenna,
                             exponent=exponent, obstruction_db=args.obstruction,
                             gps=fix, wifi_ap=access_point,
                             air_boards=args.air_boards,
                             air_quantum_ns=args.air_quantum_us * 1000,
                             audio=args.audio, mic=args.mic, air_name=tag)
        if device is not None and device.has_sdcard:
            # The device's card, in place of the fresh empty one the board
            # built. Its contents go back to the folder when the run ends
            # (ADR 0133).
            from emu.devices.sdcard import SdCard
            sd = next((d for d in hw.spi.devices.values()
                       if isinstance(d, SdCard)), None)
            if sd is not None:
                sd.image[:] = device_folder.load_sdcard(device, len(sd.image))
                # What was loaded, so an unchanged card is not written back.
                card_loaded = hashlib.sha256(bytes(sd.image)).hexdigest()
        hw.serial_in = serial_input
        if hw.usb_otg is not None:
            # Typed to the OTG port: the host on its cable sends it (ADR 0166).
            serial_input.route("otg", hw.usb_otg.type_bytes)
            if bridge is not None and bridge.port == "otg":
                # Read where the log grows, not its copy (ADR 0175).
                bridge.log_path = hw.usb_otg.live_path
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
    print(f"psram   : {psram_mb} MB, {psram_why}")
    if access_point:
        print(f"wifi-ap : {access_point[0]!r} every {access_point[1]} ms on "
              f"channel {access_point[2]}"
              + (", WPA2-PSK" if access_point[3] else ", open")
              + (", uplinked to the internet" if access_point[5] else ""))
        for host_port, device_port in access_point[6]:
            print(f"forward : localhost:{host_port} reaches the device's "
                  f"port {device_port} over WiFi")
    if hw_module.SOC_DEVICES:
        print(f"soc     : {', '.join(hw_module.SOC_DEVICES)}")
    print(f"uart    : {uart}")
    print(f"usb     : {usb}")
    if bridge is not None:
        print(f"serial  : the {bridge.port} port on tcp port {bridge.address[1]} "
              "(ADR 0158)")
    started = time.monotonic()
    interrupted = False
    mirror.start()
    try:
        reason, wall = run_qemu(cmd, mirror.local(uart), elog, args.timeout, args.stall,
                                server, usb=mirror.local(usb))
    except KeyboardInterrupt:
        # Stopped from outside, which is a normal way for a run to end when
        # something is watching it and has seen enough. The manifest is the
        # record of what happened, so it is worth as much here as anywhere.
        interrupted = True
        reason, wall = "stopped from outside", time.monotonic() - started
    finally:
        arm_teardown_watchdog(args.outdir)
        # QEMU has stopped: the rest of what it wrote goes to the run's folder
        # before anything reads it there (ADR 0174).
        mirror.stop()
        if server:
            _report_bus(server, tail, irq, hw)
            server.stop()
        if tail:
            tail.stop()
        if irq:
            irq.stop()
        if hw:
            if device is not None and device.has_sdcard:
                # The card goes back to the folder whatever the run was:
                # stopped, timed out or crashed (ADR 0133).
                from emu.devices.sdcard import SdCard
                sd = next((d for d in hw.spi.devices.values()
                           if isinstance(d, SdCard)), None)
                if sd is not None and card_loaded is not None:
                    device_folder.save_sdcard(device, sd.image, card_loaded)
            hw.stop()
        faulthandler.cancel_dump_traceback_later()
        if bridge is not None:
            bridge.close()
        serial_input.close()

    radio_stats: dict = {}
    backlight_stats: dict = {}
    unmodelled_stats: dict = {}
    if hw is not None:
        for spi_device in getattr(hw.spi, "devices", {}).values():
            if hasattr(spi_device, "sent") and hasattr(spi_device, "heard"):
                radio_stats = {
                    "commands": getattr(spi_device, "commands", 0),
                    "sent": spi_device.sent,
                    "heard": spi_device.heard,
                    "collisions": getattr(spi_device, "collisions", 0),
                    "out_of_range": getattr(spi_device, "out_of_range", 0),
                    "wrong_frequency": getattr(spi_device, "wrong_frequency", 0),
                    "deaf": getattr(spi_device, "deaf", 0),
                    "not_listening": getattr(spi_device, "not_listening", 0),
                    "cut_off": getattr(spi_device, "cut_off", 0),
                    "cad_sweeps": getattr(spi_device, "cad_sweeps", 0),
                    "cad_detected": getattr(spi_device, "cad_detected", 0),
                }
        # Every claimed window and what the firmware did with it. The run
        # already prints the busiest few; keeping the whole list in the record
        # is what makes "which hardware is missing" a question a run answers
        # rather than one to go and grep bus logs for afterwards (ADR 0053).
        unmodelled_stats = {r.name: r.accesses for r in hw.regions if r.accesses}
        lit = hw.region("ledc")
        if lit is not None and hasattr(lit, "levels"):
            backlight_stats = {
                f"channel{index}": {
                    "now": lit.brightness(index),
                    "lowest": min(lit.levels[index]),
                    "highest": max(lit.levels[index]),
                    "changes": len(lit.levels[index]),
                }
                for index in lit.driving() if lit.levels[index]
            }

    result = RunResult(
        qemu_version=qemu_version(qemu),
        argv=cmd,
        seed=seed,
        inputs={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (flash, efuse)},
        exit_reason=reason,
        wall_seconds=round(wall, 2),
        device=str(device.root) if device is not None else None,
        uart_bytes=uart.stat().st_size if uart.exists() else 0,
        uart_path=str(uart),
        usb_bytes=usb.stat().st_size if usb.exists() else 0,
        usb_path=str(usb),
        qemu_log_path=str(qlog),
        stderr_log_path=str(elog),
        traced=args.trace,
    )
    if args.trace:
        result.notes.append("timing is meaningless in a traced run")
    result.radio = radio_stats
    result.backlight = backlight_stats
    result.unmodelled = unmodelled_stats
    if hw is not None and hw.usb_otg is not None:
        result.usb_otg = hw.usb_otg.summary()
        result.usb_otg_bytes = hw.usb_otg.bytes_in

    manifest = args.outdir / f"{tag}.manifest.json"
    manifest.write_text(json.dumps(asdict(result), indent=2))

    print(f"result  : {reason} after {wall:.1f}s, {result.uart_bytes} bytes of UART, "
          f"{result.usb_bytes} of USB")
    if result.usb_otg and result.usb_otg_bytes:
        print(f"          {result.usb_otg}")
    print(f"manifest: {manifest}\n")
    if interrupted:
        return 0
    if uart.exists() and result.uart_bytes:
        print("--- last UART lines " + "-" * 40)
        tail = uart.read_text(errors="replace").splitlines()[-12:]
        for line in tail:
            print("  " + line)
        print("-" * 60)
    if usb.exists() and result.usb_bytes:
        print("--- last USB lines " + "-" * 41)
        for line in usb.read_text(errors="replace").splitlines()[-12:]:
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
        # The same question the I2C buses answer below: what did the firmware
        # talk to that nothing here answers?
        if hw.spi.unmodelled:
            empty = ", ".join(f"select {cs} ({n} segment(s))"
                              for cs, n in sorted(hw.spi.unmodelled.items()))
            print(f"    clocked into nothing: {empty}")
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
        backlight = hw.region("ledc")
        if backlight is not None and hasattr(backlight, "summary"):
            print(f"  {backlight.summary()}")

        if hw.gps:
            print(f"  gps: {hw.gps.bytes_in} byte(s) in, {hw.gps.bytes_out} out")
            print(f"    {hw.gps.device.summary()}")
        if hw.modem:
            print(f"  modem: {hw.modem.bytes_in} byte(s) in, {hw.modem.bytes_out} out")
            print(f"    {hw.modem.device.summary()}")
        if hw.clock:
            print(f"  guest clock: {hw.clock.ticks} tick(s)")

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
