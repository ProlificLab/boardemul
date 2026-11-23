#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Meshtastic's client API on the T-Deck's USB OTG port (ADR 0166).

Meshtastic's `Serial` on the T-Deck is TinyUSB's CDC port on the S3's OTG
controller. The run puts that port on TCP, and tools/mesh_api.py asks the node
for its configuration over the stream protocol, as `meshtastic --port` would
through a cable: the node answers with its number, its network settings and
its display mode.

    python3 -m pytest tests/test_meshtastic_api.py -m slow
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, Run, qemu_available, run_emulator,
)
from tools import mesh_api  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.alone,      # deadlines kept on the host's time (ADR 0167)
    pytest.mark.skipif(not FIRMWARE_MESHTASTIC.exists(), reason="Meshtastic image not fetched"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _otg(outdir: Path) -> str:
    path = outdir / "t.otg.log"
    return path.read_text(errors="replace") if path.is_file() else ""


@pytest.fixture(scope="module")
def answered(tmp_path_factory) -> tuple[Run, dict]:
    port = _free_port()
    seen: dict = {}

    def ask(outdir: Path) -> None:
        deadline = time.monotonic() + 300
        while "Booted" not in _otg(outdir):
            if time.monotonic() > deadline:
                return
            time.sleep(1)
        # Meshtastic restarts itself once on a first boot; ask until a whole
        # configuration comes back.
        while time.monotonic() < deadline and "my_node_num" not in seen:
            time.sleep(10)
            try:
                node = mesh_api.Node(f"127.0.0.1:{port}", timeout=60)
                seen.update(node.configuration())
                node.sock.close()
            except OSError:
                pass

    run = run_emulator(tmp_path_factory.mktemp("meshapi"), firmware=FIRMWARE_MESHTASTIC,
                       board="tdeck", timeout=420,
                       overlays=(f"0xc90000={MESHTASTIC_LITTLEFS}",),
                       extra=("--psram-octal", "--serial-tcp", f"{port}:otg"),
                       script=ask)
    return run, seen


def test_the_log_is_on_the_otg_port(answered) -> None:
    run, _ = answered
    otg = _otg(run.outdir)
    assert "Booted" in otg, otg[-600:]


def test_the_node_answers_with_its_configuration(answered) -> None:
    _, seen = answered
    assert seen.get("my_node_num"), seen
    assert "network" in seen, seen
    # The T-Deck's colour interface is its default (DisplayMode.COLOR).
    assert seen.get("display_mode") == "color", seen
