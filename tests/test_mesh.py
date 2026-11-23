#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
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

from emu.devices import lora  # noqa: E402
from emu.devices.air import AirMedium  # noqa: E402
from tests.support import (  # noqa: E402
    BOOT_BANNER, FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, REPO_ROOT,
    qemu_available,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="no Meshtastic build"),
    pytest.mark.skipif(not MESHTASTIC_LITTLEFS.exists(), reason="no filesystem image"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

CONFIGURED = REPO_ROOT / "build" / "littlefs-configured.bin"
OVERLAY_AT = 0xC90000
# A ceiling, not a plan. The test stops the nodes as soon as the evidence its
# assertions need is in (ADR 0040): the listener has heard both of them, and one
# has received a packet from the other. Both have to be waited for -- two nodes
# that boot together can put their first broadcasts on top of each other, each
# deaf while it transmits, so "both have spoken" can come well before "a packet
# crossed" (ADR 0098). This ceiling only matters on a host too slow for either:
# with sixteen guests on fourteen cores a node was seen to send nothing in
# 300 s, so it is twice that (ADR 0141).
RUN_SECONDS = 600
# A moment after the evidence, so a node that has just received is not stopped
# in the middle of answering.
GRACE_SECONDS = 2
RECEIVED = "sx1262: heard a"


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
    """One node, allowed to reboot. Its firmware can overflow ipc1's stack
    while installing the radio's interrupt, and then panics and resets, as a
    T-Deck does; the board comes back, and the mesh is judged on what it does
    after (ADR 0099). How often each node booted is reported, not hidden."""
    outdir = work / node
    (outdir / "build").mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "emu/run.py", str(FIRMWARE_MESHTASTIC),
         "--board", "tdeck", "--bus", "--psram-octal", "--reboot",
         "--air", str(air), "--mac", mac,
         "--builddir", str(outdir / "build"),
         "--overlay", f"0x{OVERLAY_AT:x}={overlay}",
         "--outdir", str(outdir), "--tag", node,
         "--timeout", str(RUN_SECONDS), "--stall", str(RUN_SECONDS)],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _a_node_received(work: Path, nodes: dict) -> bool:
    """Whether either node's radio has logged receiving a packet. The radio
    writes one line per packet it receives, which is what 'a packet crossed'
    looks like while the run is still going; its counters only reach the
    manifest when it stops."""
    for name in nodes:
        log = work / name / f"{name}.bus.log"
        try:
            if RECEIVED in log.read_text(errors="replace"):
                return True
        except OSError:
            continue
    return False


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
        senders: set[str] = set()
        while time.monotonic() < deadline:
            for board, frame in listener.poll_named():
                # Who sent a frame is the air's to say: the board it came from.
                # Counting frames would stop as soon as one node had spoken
                # twice (ADR 0098), and counting the packet's `from` counts a
                # node that never spoke whenever the other relays its packet,
                # or sends it by mistake (ADR 0141).
                if lora.decode(frame) is not None:
                    senders.add(board)
            if len(senders) >= len(nodes) and _a_node_received(work, nodes):
                time.sleep(GRACE_SECONDS)
                break
            # With --reboot a node's reset does not end its run, so this is
            # only a crash inside QEMU or a run that hit its own ceiling.
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

    radios, boots = {}, {}
    for name in nodes:
        manifest = work / name / f"{name}.manifest.json"
        if not manifest.exists():
            pytest.fail(f"node {name} left no manifest")
        radios[name] = json.loads(manifest.read_text()).get("radio", {})
        uart = work / name / f"{name}.uart.log"
        boots[name] = (uart.read_text(errors="replace").count(BOOT_BANNER)
                       if uart.exists() else 0)
    return {"radios": radios, "senders": senders, "boots": boots}


def test_both_nodes_transmit(two_nodes) -> None:
    """A node with a region set keys its radio on its own. If neither does,
    nothing below this means anything."""
    for name, radio in two_nodes["radios"].items():
        assert radio.get("sent", 0) >= 1, (
            f"node {name} never transmitted: {radio}; "
            f"boots {two_nodes['boots']}")


def test_the_listener_heard_both_nodes(two_nodes) -> None:
    """The early stop's own evidence, asserted. If the listener stops telling the
    two senders apart, the run silently waits out its whole ceiling instead of
    stopping -- which went unnoticed for three weeks -- so it fails here."""
    senders = two_nodes["senders"]
    assert senders == {"one", "two"}, (
        f"the listener heard {sorted(senders)}; boots {two_nodes['boots']}")


def test_a_packet_crosses_between_them(two_nodes) -> None:
    """The mesh itself: one board received what the other put on the air."""
    radios = two_nodes["radios"]
    heard = {name: radio.get("heard", 0) for name, radio in radios.items()}
    assert sum(heard.values()) >= 1, (
        f"neither node heard the other: {radios}; boots {two_nodes['boots']}")


def test_nothing_was_lost_to_range_or_tuning(two_nodes) -> None:
    """Standing in the same place and on the same channel, the only reason a
    packet should go missing is a collision -- which is a real thing two nodes
    talking at once do, so it is not asserted against."""
    for name, radio in two_nodes["radios"].items():
        assert radio.get("out_of_range", 0) == 0, f"node {name}: {radio}"
        assert radio.get("wrong_frequency", 0) == 0, f"node {name}: {radio}"
