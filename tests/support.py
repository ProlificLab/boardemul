#!/usr/bin/env python3
"""Helpers shared by the tests.

Includes a minimal PNG reader, which the emulator itself has no need for -- it
only writes them -- but a test that asserts on what was drawn does. It also
holds the runner the slow tests drive the real emulator with, so that adding a
board means adding a test file rather than copying a subprocess invocation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIRMWARE = REPO_ROOT / "firmware" / "T-Deck-MAX_V3.1_20260313.bin"
FIRMWARE_TDECK = REPO_ROOT / "firmware" / "T-Deck_UnitTest_251113.bin"
# Every vendor build of the T-Deck test firmware on disk. The bring-up walls
# (ADR 0013, 0016, 0019, 0020) were read off the 250620 build; the slow test
# runs each build present so that a gate specific to one of them would show.
FIRMWARE_TDECK_BUILDS = sorted((REPO_ROOT / "firmware").glob("T-Deck_UnitTest_2*.bin"))
FIRMWARE_MESHTASTIC = REPO_ROOT / "firmware" / "firmware-t-deck-tft-2.7.10.94d4bdf.bin"
MESHTASTIC_LITTLEFS = REPO_ROOT / "firmware" / "littlefs-t-deck-tft-2.7.10.94d4bdf.bin"
# Not vendor firmware: built from tools/restart_probe by its build.py, so it
# is neither downloaded nor pinned, and its test skips until it is built
# (ADR 0123).
FIRMWARE_RESTART_PROBE = REPO_ROOT / "firmware" / "restart_probe.bin"
# Built the same way, from tools/matrix_reset_probe (ADR 0142).
FIRMWARE_MATRIX_RESET_PROBE = REPO_ROOT / "firmware" / "matrix_reset_probe.bin"


PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def png_colour_type(path: Path) -> int:
    """0 for greyscale, 2 for truecolour, straight out of the IHDR."""
    data = Path(path).read_bytes()
    return data[8 + 8 + 9]


def read_png(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit PNG, greyscale or truecolour, to (width, height, bytes).

    Only the shape our own writer produces: one IDAT, no interlace, no palette.
    Anything else is a bug in the writer, so it raises rather than coping. The
    returned buffer is one byte per pixel for greyscale and three for colour.
    """
    data = Path(path).read_bytes()
    if data[:8] != PNG_MAGIC:
        raise ValueError("not a PNG")

    pos = 8
    width = height = 0
    channels = 1
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            if depth != 8 or colour not in (0, 2):
                raise ValueError(f"expected 8-bit grey or RGB, got {depth}/{colour}")
            channels = 3 if colour == 2 else 1
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break

    raw = zlib.decompress(idat)
    stride = width * channels
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        filter_type = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        # The filters predict from the pixel to the left, which is `channels`
        # bytes back, not one -- the same code has to serve both formats.
        if filter_type == 1:            # Sub
            for x in range(channels, stride):
                line[x] = (line[x] + line[x - channels]) & 0xFF
        elif filter_type == 2:          # Up
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 0xFF
        elif filter_type == 3:          # Average
            for x in range(stride):
                left = line[x - channels] if x >= channels else 0
                line[x] = (line[x] + ((left + prev[x]) >> 1)) & 0xFF
        elif filter_type == 4:          # Paeth
            for x in range(stride):
                a = line[x - channels] if x >= channels else 0
                b = prev[x]
                c = prev[x - channels] if x >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pred) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unsupported PNG filter {filter_type}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, bytes(out)


def ink_below_status_bar(path: Path, status_rows: int = 30) -> int:
    """Count dark pixels, ignoring the status bar.

    The clock and charge indicator change between refreshes, so a whole-frame
    hash is not a stable assertion. What sits below it is.
    """
    width, height, pixels = read_png(path)
    body = pixels[status_rows * width:]
    return sum(1 for p in body if p < 0x80)


def region_digest(path: Path, status_rows: int = 30) -> str:
    import hashlib

    width, height, pixels = read_png(path)
    return hashlib.sha256(pixels[status_rows * width:]).hexdigest()[:16]


# -- driving the real emulator ----------------------------------------------


# The emulator's --timeout bounds guest time; the process then still has to stop
# QEMU, drain the bus server and write its report. Waiting exactly --timeout for
# it to exit races that shutdown, and only firmware that runs to the timeout --
# rather than stalling early -- ever loses the race.
SHUTDOWN_GRACE = 30


def send_touch(outdir: Path, x: int, y: int, hold: float = 1.2) -> None:
    """One tap on the glass, in display coordinates. The board turns these into
    the touch controller's own raster (ADR 0030)."""
    events = outdir / "input.jsonl"
    with events.open("a") as fh:
        fh.write(json.dumps({"type": "touch", "x": x, "y": y, "down": True}) + chr(10))
    time.sleep(hold)
    with events.open("a") as fh:
        fh.write(json.dumps({"type": "touch", "down": False}) + chr(10))


def send_text(outdir: Path, text: str) -> None:
    """Characters, which the board types as the chords the device needs: a
    digit on the T-Deck is SYM held with a letter (ADR 0127)."""
    with (outdir / "input.jsonl").open("a") as fh:
        fh.write(json.dumps({"type": "text", "text": text}) + chr(10))


def send_touchkey(outdir: Path, key: int, hold: float = 1.5) -> None:
    """One of the Max's glass touch keys, pressed and let go (ADR 0131)."""
    events = outdir / "input.jsonl"
    with events.open("a") as fh:
        fh.write(json.dumps({"type": "touchkey", "key": key, "down": True}) + chr(10))
    time.sleep(hold)
    with events.open("a") as fh:
        fh.write(json.dumps({"type": "touchkey", "key": key, "down": False}) + chr(10))


def pixel(path: Path, x: int, y: int) -> tuple[int, int, int]:
    width, _, pixels = read_png(path)
    at = 3 * (y * width + x)
    return pixels[at], pixels[at + 1], pixels[at + 2]


def count_pixels(path: Path, box: tuple[int, int, int, int], test) -> int:
    """How many pixels in a box satisfy a colour test.

    Sampling one pixel to identify a control is a trap: the middle of a button
    is its label, not its face, so a blue button reads back white. Counting a
    region says what is there whatever the lettering does.
    """
    x0, y0, x1, y1 = box
    width, _, pixels = read_png(path)
    count = 0
    for y in range(y0, y1):
        row = 3 * y * width
        for x in range(x0, x1):
            at = row + 3 * x
            if test(pixels[at], pixels[at + 1], pixels[at + 2]):
                count += 1
    return count


def dark_pixels(path: Path, box: tuple[int, int, int, int]) -> int:
    """How much ink is inside a box. That is how text typed into a white field
    shows up without needing a golden image of a font."""
    return count_pixels(path, box, lambda r, g, b: r < 128 and g < 128 and b < 128)


def wait_for_panel(outdir: Path, predicate, timeout: float) -> bool:
    """Wait until the drawn panel satisfies a predicate, or give up.

    An interaction test has to act on what is on screen, not on a stopwatch:
    the guest's speed varies with the host, and acting early sends taps into a
    boot screen that swallows them.
    """
    latest = outdir / "frames" / "latest.png"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if latest.exists():
            try:
                if predicate(latest):
                    return True
            except (OSError, ValueError, struct.error, zlib.error):
                pass        # caught mid-write; look again next tick
        time.sleep(1.0)
    return False


def qemu_available() -> bool:
    local = REPO_ROOT / "third_party" / "qemu" / "build" / "qemu-system-xtensa"
    return bool(os.environ.get("BOARDEMUL_QEMU")
                or local.is_file()
                or shutil.which("qemu-system-xtensa"))


@dataclass
class Run:
    outdir: Path
    frames: list[Path] = field(default_factory=list)
    uart: str = ""
    # What the host on the chip's USB serial port received (ADR 0100). A
    # firmware whose Serial is that port says almost nothing on the UART.
    usb: str = ""

    def digests(self) -> list[str]:
        return [region_digest(f) for f in self.frames]

    @property
    def boots(self) -> int:
        """How many times the chip came out of reset. Once, unless the run let
        it reboot (ADR 0099) and it did."""
        return self.uart.count(BOOT_BANNER)

    @property
    def last_uart(self) -> str:
        """What the guest said on its UART since it last came out of reset:
        what a test that lets the board reboot judges it on (ADR 0125)."""
        return last_boot(self.uart)

    @property
    def last_usb(self) -> str:
        """The same, on the USB serial port, where the ROM's banner goes too
        (ADR 0114)."""
        return last_boot(self.usb)


# -- boots and panics ---------------------------------------------------------

# The ROM prints this once per boot, with the reset cause after it.
BOOT_BANNER = "rst:0x"

# The one panic a run may reboot from (ADR 0099, 0114): ipc1's 1024-byte stack
# overflowing by four bytes as the GPIO ISR-service install ends its heap lock
# with a tick pending. The overflow is the firmware's, silicon hits it too, and
# a board that does comes back. Anything else that panics is still a failure.
IPC1_CANARY = re.compile(
    r"Guru Meditation Error: Core +1 panic'ed \(Unhandled debug exception\)\.\s*"
    r"Debug exception reason: Stack canary watchpoint triggered \(ipc1\)")


def by_boot(text: str) -> list[str]:
    """What was said in each boot, in order, each from its ROM banner on.

    The ROM's two lines before a banner go with the boot before it, which is
    harmless: nothing asserts on them.
    """
    starts = [m.start() for m in re.finditer(re.escape(BOOT_BANNER), text)][1:]
    edges = [0] + starts + [len(text)]
    return [text[a:b] for a, b in zip(edges, edges[1:])]


def last_boot(text: str) -> str:
    """What was said since the chip last came out of reset. A board that
    rebooted is judged on this, as the device in a hand would be."""
    return by_boot(text)[-1]


def panics(text: str) -> list[str]:
    """Each panic, as its opening line and the line after it -- which for a
    debug exception is the reason, and is what tells one cause from another."""
    return re.findall(r"Guru Meditation Error:[^\n]*\n?[^\n]*", text)


def unforgiven(text: str) -> list[str]:
    """Each boot's panic, where it is not ADR 0099's ipc1 canary.

    A boot is judged by its first panic, which is why it ended. Any after it in
    the same boot come from the panic handler itself: on 250620 the canary's
    core dump faults, prints a second Guru Meditation (LoadProhibited), and
    says "Re-entered core dump!" before the reset (ADR 0114).
    """
    firsts = [found[0] for found in map(panics, by_boot(text)) if found]
    return [p for p in firsts if not IPC1_CANARY.match(p)]


def unexplained_resets(text: str) -> list[int]:
    """The boots, numbered from 1, that ended for a reason other than the
    forgiven panic. Before a run could reboot, any reset ended it; now each
    one has to be accounted for (ADR 0114).

    A boot that said nothing past the ROM's banner is not excused. It was one
    reboot from core 1 printing two banners, because QEMU let core 0 run while
    the restart had it stalled, and ADR 0123 fixed that, so another one means
    the restart has gone wrong again.
    """
    def ended_in_the_canary(boot: str) -> bool:
        first = panics(boot)[:1]
        return bool(first) and IPC1_CANARY.match(first[0]) is not None

    return [n for n, boot in enumerate(by_boot(text)[:-1], 1)
            if not ended_in_the_canary(boot)]


def reset_trouble(text: str) -> str:
    """Why a run's resets are not all the forgiven panic, or "" when they are:
    ADR 0114's two checks in one message, for the tests ADR 0125 lets reboot.
    The message carries the boot that ended badly."""
    wrong = unforgiven(text)
    if wrong:
        return f"a panic other than ADR 0099's ipc1 canary: {wrong[0]!r}"
    unexplained = unexplained_resets(text)
    if unexplained:
        boots = by_boot(text)
        return (f"boot {unexplained[0]} of {len(boots)} ended without the ipc1 "
                "canary panic: " + boots[unexplained[0] - 1][-1500:])
    return ""


def _uart(outdir: Path) -> str:
    """Whatever the guest has said so far. Read again each time: a run being
    waited on is still writing to it."""
    for log in outdir.glob("*.uart.log"):
        try:
            return log.read_text(errors="replace")
        except OSError:
            return ""
    return ""


def usb_text(outdir: Path) -> str:
    """What the host on the USB serial port has received so far."""
    for log in outdir.glob("*.usb.log"):
        try:
            return log.read_text(errors="replace")
        except OSError:
            return ""
    return ""


def radio_busy(outdir: Path) -> tuple[int, int] | None:
    """The radio's BUSY line as the SPI controller last reported it (ADR
    0111): how many holds, and the longest in microseconds. None when it
    never reported one."""
    for log in outdir.glob("*.qemu.log"):
        found = re.findall(r"select 1's busy line: (\d+) hold\(s\), the "
                           r"longest (\d+) us", log.read_text(errors="replace"))
        if found:
            holds, longest = found[-1]
            return int(holds), int(longest)
    return None


def audio_streams(outdir: Path) -> list[tuple[str, str, float, int, int]]:
    """Each I2S stream the controller paced by its sample clock (ADR 0119):
    block, direction, sample rate in Hz, channels and bits, in the order they
    started."""
    streams = []
    for log in outdir.glob("*.qemu.log"):
        for block, direction, hz, channels, bits in re.findall(
                r"esp-i2s-master\((\w+)\): (receive|transmit) at ([0-9.]+) Hz, "
                r"(\d+) channel\(s\) of (\d+) bit\(s\)", log.read_text(errors="replace")):
            streams.append((block, direction, float(hz), int(channels), int(bits)))
    return streams


def bus_clocks(outdir: Path) -> dict[str, list[float]]:
    """Every clock each bus ran at, in the order the controllers reported them
    (ADRs 0120, 0121): "spi2:1" for an SPI controller's select, "i2c0" for an
    I2C controller's SCL."""
    clocks: dict[str, list[float]] = {}
    for log in outdir.glob("*.qemu.log"):
        text = log.read_text(errors="replace")
        for bus, cs, hz in re.findall(
                r"esp-spi-master\((\w+)\): select (\d+) clocked at ([0-9.]+) Hz", text):
            clocks.setdefault(f"{bus}:{cs}", []).append(float(hz))
        for bus, hz in re.findall(r"esp-i2c-master\((\w+)\): SCL at ([0-9.]+) Hz", text):
            clocks.setdefault(bus, []).append(float(hz))
    return clocks


def _frames(outdir: Path) -> list[Path]:
    return sorted((outdir / "frames").glob("frame-*.png"))


def run_emulator(outdir: Path, *, firmware: Path = FIRMWARE,
                 board: str = "tdeck-max", tap: tuple[int, int] | None = None,
                 wait_frames: int = 0, timeout: int = 150,
                 settle: int = 25, overlays: tuple[str, ...] = (),
                 icount: bool = False, extra: tuple[str, ...] = (),
                 script=None, until=None, env: dict[str, str] | None = None) -> Run:
    """Boot a firmware, optionally tapping the panel once it has drawn.

    A board with no panel model draws nothing, so waiting on frames is opt-in:
    wait_frames=0 means run to the timeout and report what the UART said.

    `until(uart, frames)` is how a test says what it came to see. A boot that
    has shown it has nothing left to prove, and waiting out the rest of the
    timeout is time spent watching an idle guest -- which was most of the time
    this suite took. Without it a run still goes to its timeout, except after a
    tap or a script, where the interaction finishing is the same statement.
    """
    proc = subprocess.Popen(
        [sys.executable, "emu/run.py", str(firmware), "--bus", "--board", board,
         "--tag", "t", "--outdir", str(outdir), "--timeout", str(timeout),
         # Its own flash image. The default is build/<firmware>/, shared by
         # every run of that firmware, and building one rewrites the file a
         # still-running guest has open -- which shows up as the guest dying on
         # a stack canary, nowhere near the cause. Two tests booting the same
         # build is all it takes, and the mesh tests learned this already
         # (ADR 0036).
         "--builddir", str(outdir / "build"),
         "--stall", str(timeout)]
        + [arg for spec in overlays for arg in ("--overlay", spec)]
        + (["--icount"] if icount else [])
        + list(extra),
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        env=dict(os.environ, **(env or {})))
    try:
        deadline = time.monotonic() + timeout
        while wait_frames and time.monotonic() < deadline:
            if len(_frames(outdir)) >= wait_frames:
                break
            if proc.poll() is not None:
                break
            time.sleep(1)

        if tap is not None:
            events = outdir / "input.jsonl"
            with events.open("a") as fh:
                fh.write(json.dumps({"type": "touch", "x": tap[0],
                                     "y": tap[1], "down": True}) + "\n")
                fh.flush()
                time.sleep(0.8)
                fh.write(json.dumps({"type": "touch", "down": False}) + "\n")
                fh.flush()
            # A refresh takes a couple of seconds of emulated time; give the
            # firmware room to redraw before looking.
            time.sleep(settle)

        if script is not None:
            # A whole interaction, not one tap: the caller waits on what is
            # drawn and sends touches and keys in turn. It runs while the guest
            # does, and the finally below still tears the run down if it raises.
            script(outdir)

        done = until is not None or script is not None or tap is not None
        if not done:
            proc.wait(timeout=timeout + SHUTDOWN_GRACE)
        else:
            deadline = time.monotonic() + timeout + SHUTDOWN_GRACE
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break               # the guest stopped on its own
                if until is None or until(_uart(outdir), _frames(outdir)):
                    break
                time.sleep(0.5)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    return Run(outdir=outdir, frames=_frames(outdir), uart=_uart(outdir),
               usb=usb_text(outdir))
