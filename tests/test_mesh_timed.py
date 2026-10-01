#!/usr/bin/env python3
"""Two boards on one timed air (ADR 0113).

On a timed air every board advances a quantum at a time with the others,
under an instruction-counted clock, so a packet one board sends is judged by
the other on the same clock it was sent on. What that buys is checked here
the way the air's physics says it must come out: two copies of unmodified
Meshtastic reach one another, and every packet heard ends exactly when the
packet its sender put on the air ended -- to the microsecond, whatever the
host was doing meanwhile.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, REPO_ROOT, qemu_available,
)
from tests.test_mesh import OVERLAY_AT, configured_filesystem  # noqa: E402,F401

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

# A ceiling, not a plan: the run stops once a packet has crossed. The first
# exchange comes at about 38 s of guest time, which a timed air reaches in a
# few minutes of wall time.
RUN_SECONDS = 900
GRACE_SECONDS = 5
NODES = {"one": "24:6F:28:00:00:11", "two": "24:6F:28:00:00:22"}
SENT = re.compile(r"sx1262: sent a (\d+)-byte packet at guest ([0-9.]+) s for ([0-9.]+) ms")
HEARD = re.compile(r"sx1262: heard a (\d+)-byte packet .*at guest ([0-9.]+) s")


def _launch(node: str, work: Path, air: Path, mac: str, overlay: Path):
    outdir = work / node
    (outdir / "build").mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
         "--board", "tdeck", "--bus", "--psram-octal", "--reboot",
         "--air", str(air), "--air-boards", str(len(NODES)), "--icount",
         "--mac", mac, "--builddir", str(outdir / "build"),
         "--overlay", f"0x{OVERLAY_AT:x}={overlay}",
         "--outdir", str(outdir), "--tag", node,
         "--timeout", str(RUN_SECONDS), "--stall", str(RUN_SECONDS)],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _log(work: Path, node: str) -> str:
    try:
        return (work / node / f"{node}.bus.log").read_text(errors="replace")
    except OSError:
        return ""


@pytest.fixture(scope="module")
def timed_air(tmp_path_factory, configured_filesystem: Path) -> dict:
    work = tmp_path_factory.mktemp("timed")
    (work / "air").mkdir()
    running = {node: _launch(node, work, work / "air", mac, configured_filesystem)
               for node, mac in NODES.items()}
    try:
        deadline = time.monotonic() + RUN_SECONDS
        while time.monotonic() < deadline:
            if any(HEARD.search(_log(work, node)) for node in NODES):
                time.sleep(GRACE_SECONDS)
                break
            if all(p.poll() is not None for p in running.values()):
                break
            time.sleep(2)
    finally:
        for proc in running.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
    result = {"sent": {}, "heard": {}, "radios": {}}
    for node in NODES:
        text = _log(work, node)
        result["sent"][node] = [(int(size), float(at), float(ms) / 1000)
                                for size, at, ms in SENT.findall(text)]
        result["heard"][node] = [(int(size), float(at)) for size, at in HEARD.findall(text)]
        manifest = work / node / f"{node}.manifest.json"
        result["radios"][node] = (json.loads(manifest.read_text()).get("radio", {})
                                  if manifest.exists() else {})
    return result


def test_both_nodes_transmit(timed_air) -> None:
    for node, sent in timed_air["sent"].items():
        assert sent, f"node {node} never transmitted: {timed_air['radios']}"


def test_a_packet_crosses(timed_air) -> None:
    assert any(timed_air["heard"].values()), (
        f"neither node heard the other: {timed_air['radios']}")


def test_a_packet_is_heard_when_its_last_bit_left_the_sender(timed_air) -> None:
    """The shared clock, checked by its physics: the receiver's RxDone falls
    at the sender's start plus the time on air, on one timeline (ADR 0113)."""
    for node, heard in timed_air["heard"].items():
        others = [packet for other, sent in timed_air["sent"].items()
                  if other != node for packet in sent]
        for size, at in heard:
            ends = [start + airtime for sent_size, start, airtime in others
                    if sent_size == size]
            assert any(abs(at - end) < 2e-6 for end in ends), (
                f"{node} heard a {size}-byte packet at {at:.6f} s, which no "
                f"packet sent ended at: {others}")
