#!/usr/bin/env python3
"""Two boards on one timed air (ADR 0113), twice over (ADR 0115).

On a timed air every board advances a quantum at a time with the others,
under an instruction-counted clock, so a packet one board sends is judged by
the other on the same clock it was sent on. What that buys is checked the way
the air's physics says it must come out: two copies of unmodified Meshtastic
reach one another, and every packet heard ends exactly when the packet its
sender put on the air ended.

And it is the same run every time. The same two nodes run on a second air at
the same moment -- so the host is busier than either would make it alone --
and both airs must carry the same packets at the same guest times, to the
microsecond.
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

# A ceiling, not a plan: each air stops once a packet has crossed on it. The
# first exchange comes at about 38 s of guest time, which a timed air reaches
# in a few minutes of wall time.
RUN_SECONDS = 900
GRACE_SECONDS = 5
NODES = {"one": "24:6F:28:00:00:11", "two": "24:6F:28:00:00:22"}
AIRS = ("a", "b")
SENT = re.compile(r"sx1262: sent a (\d+)-byte packet at guest ([0-9.]+) s for ([0-9.]+) ms")
HEARD = re.compile(r"sx1262: heard a (\d+)-byte packet .*at guest ([0-9.]+) s")


def _launch(node: str, work: Path, mac: str, overlay: Path):
    outdir = work / node
    (outdir / "build").mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
         "--board", "tdeck", "--bus", "--psram-octal", "--reboot",
         "--air", str(work / "air"), "--air-boards", str(len(NODES)), "--icount",
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


def _record(work: Path) -> dict:
    result = {"sent": {}, "heard": {}, "radios": {}, "events": []}
    for node in NODES:
        text = _log(work, node)
        sent = [(int(size), float(at), float(ms) / 1000)
                for size, at, ms in SENT.findall(text)]
        heard = [(int(size), float(at)) for size, at in HEARD.findall(text)]
        result["sent"][node], result["heard"][node] = sent, heard
        result["events"] += [(at, node, "sent", size) for size, at, _ in sent]
        result["events"] += [(at, node, "heard", size) for size, at in heard]
        manifest = work / node / f"{node}.manifest.json"
        result["radios"][node] = (json.loads(manifest.read_text()).get("radio", {})
                                  if manifest.exists() else {})
    result["events"].sort()
    return result


@pytest.fixture(scope="module")
def two_airs(tmp_path_factory, configured_filesystem: Path) -> dict:
    works = {air: tmp_path_factory.mktemp(f"timed-{air}") for air in AIRS}
    running = {}
    for air, work in works.items():
        (work / "air").mkdir()
        for node, mac in NODES.items():
            running[(air, node)] = _launch(node, work, mac, configured_filesystem)
    stopped: set[str] = set()
    try:
        deadline = time.monotonic() + RUN_SECONDS
        while time.monotonic() < deadline and len(stopped) < len(AIRS):
            for air, work in works.items():
                # Stopped on what the tests below look for -- a packet heard,
                # and every node having sent -- not a packet heard and a few
                # seconds of the host's time, which is no fixed amount of the
                # guest's: an air was stopped 0.26 guest seconds before its
                # second node spoke.
                logs = [_log(work, node) for node in NODES]
                if (air not in stopped and any(HEARD.search(text) for text in logs)
                        and all(SENT.search(text) for text in logs)):
                    time.sleep(GRACE_SECONDS)
                    for node in NODES:
                        running[(air, node)].terminate()
                    stopped.add(air)
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
    return {air: _record(work) for air, work in works.items()}


def test_both_nodes_transmit(two_airs) -> None:
    for node, sent in two_airs["a"]["sent"].items():
        assert sent, f"node {node} never transmitted: {two_airs['a']['radios']}"


def test_a_packet_crosses(two_airs) -> None:
    assert any(two_airs["a"]["heard"].values()), (
        f"neither node heard the other: {two_airs['a']['radios']}")


def test_a_packet_is_heard_when_its_last_bit_left_the_sender(two_airs) -> None:
    """The shared clock, checked by its physics: the receiver's RxDone falls
    at the sender's start plus the time on air, on one timeline (ADR 0113)."""
    air = two_airs["a"]
    for node, heard in air["heard"].items():
        others = [packet for other, sent in air["sent"].items()
                  if other != node for packet in sent]
        for size, at in heard:
            ends = [start + airtime for sent_size, start, airtime in others
                    if sent_size == size]
            assert any(abs(at - end) < 2e-6 for end in ends), (
                f"{node} heard a {size}-byte packet at {at:.6f} s, which no "
                f"packet sent ended at: {others}")


def test_the_same_air_twice_is_the_same_run(two_airs) -> None:
    """Up to where the earlier of the two airs was stopped, both carried the
    same packets at the same guest times, to the microsecond (ADR 0115)."""
    a, b = two_airs["a"]["events"], two_airs["b"]["events"]
    assert a and b, "an air carried nothing to compare"
    upto = min(a[-1][0], b[-1][0])
    a = [event for event in a if event[0] <= upto]
    b = [event for event in b if event[0] <= upto]
    assert a == b, f"the airs differed:\n{a}\n{b}"
