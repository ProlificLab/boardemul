#!/usr/bin/env python3
"""Two boards, one air, and a packet that crosses between them.

This is the thing the radio work was for, and until now it existed only as
runs done by hand. The medium itself is covered a level down in
`test_air.py`; what this asks is whether two copies of unmodified firmware,
each believing it is a radio node, actually reach one another.

Both nodes boot from a filesystem that already has a region set, because a
Meshtastic node without one never transmits and two silent nodes prove
nothing. That filesystem is not a checked-in artefact: `make_configured_fs.py`
produces it by driving the setup dialog, so the whole demonstration can be
rebuilt from the vendor image and nothing else.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.air import AirMedium  # noqa: E402
from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, REPO_ROOT, qemu_available,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

CONFIGURED = REPO_ROOT / "build" / "littlefs-configured.bin"
OVERLAY_AT = 0xC90000
# A ceiling, not a plan. The test listens on the air and stops the nodes as
# soon as it has heard both of them, so this only matters on a host slow enough
# that the first broadcast takes that long -- which under a loaded suite it can
# (ADR 0040).
RUN_SECONDS = 300
GRACE_SECONDS = 12


@pytest.fixture(scope="module")
def configured_filesystem() -> Path:
    """A filesystem with a region set, built if it is not already there.

    Building it means booting a node and driving its dialog, so it takes a few
    minutes the first time and no time afterwards.
    """
    if not CONFIGURED.exists():
        done = subprocess.run(
            [sys.executable, "tools/make_configured_fs.py", "-o", str(CONFIGURED)],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=900)
        if done.returncode != 0 or not CONFIGURED.exists():
            pytest.fail("could not build a configured filesystem:\n"
                        + done.stdout[-2000:] + done.stderr[-2000:])
    return CONFIGURED


def _launch(node: str, work: Path, air: Path, mac: str, overlay: Path):
    outdir = work / node
    (outdir / "build").mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
         "--board", "tdeck", "--bus", "--psram-octal",
         "--air", str(air), "--mac", mac,
         "--builddir", str(outdir / "build"),
         "--overlay", f"0x{OVERLAY_AT:x}={overlay}",
         "--outdir", str(outdir), "--tag", node,
         "--timeout", str(RUN_SECONDS), "--stall", str(RUN_SECONDS)],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


@pytest.fixture(scope="module")
def two_nodes(tmp_path_factory, configured_filesystem: Path) -> dict[str, dict]:
    """Boot two boards on one air and report what each radio did.

    They are given different MAC addresses on purpose: two nodes booted from
    one filesystem share a node number otherwise, and each then discards the
    other's packets as its own.
    """
    work = tmp_path_factory.mktemp("mesh")
    air = work / "air"
    air.mkdir()

    nodes = {"one": "24:6F:28:00:00:11", "two": "24:6F:28:00:00:22"}

    # A third station on the same air that only listens. It is how the test
    # knows the nodes have started talking without waiting out their whole
    # budget, and it transmits nothing, so neither node hears it.
    listener = AirMedium(air, "listener")
    running = {name: _launch(name, work, air, mac, configured_filesystem)
               for name, mac in nodes.items()}
    try:
        deadline = time.monotonic() + RUN_SECONDS
        overheard = 0
        while time.monotonic() < deadline:
            overheard += len(listener.poll())
            if overheard >= 2:
                # Both have been on the air; give them a moment to have read
                # each other before they are stopped.
                time.sleep(GRACE_SECONDS)
                break
            if all(p.poll() is not None for p in running.values()):
                break
            time.sleep(1)
    finally:
        listener.close()
        for proc in running.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()

    radios = {}
    for name in nodes:
        manifest = work / name / f"{name}.manifest.json"
        if not manifest.exists():
            pytest.fail(f"node {name} left no manifest")
        radios[name] = json.loads(manifest.read_text()).get("radio", {})
    return radios


def test_both_nodes_transmit(two_nodes) -> None:
    """A node with a region set keys its radio on its own. If neither does,
    nothing below this means anything."""
    for name, radio in two_nodes.items():
        assert radio.get("sent", 0) >= 1, f"node {name} never transmitted: {radio}"


def test_a_packet_crosses_between_them(two_nodes) -> None:
    """The mesh itself: one board received what the other put on the air."""
    heard = {name: radio.get("heard", 0) for name, radio in two_nodes.items()}
    assert sum(heard.values()) >= 1, f"neither node heard the other: {two_nodes}"


def test_nothing_was_lost_to_range_or_tuning(two_nodes) -> None:
    """Standing in the same place and on the same channel, the only reason a
    packet should go missing is a collision -- which is a real thing two nodes
    talking at once do, so it is not asserted against."""
    for name, radio in two_nodes.items():
        assert radio.get("out_of_range", 0) == 0, f"node {name}: {radio}"
        assert radio.get("wrong_frequency", 0) == 0, f"node {name}: {radio}"
