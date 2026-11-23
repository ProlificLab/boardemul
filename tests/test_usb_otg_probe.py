#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""TinyUSB's CDC port on the OTG controller, end to end (ADR 0166).

The probe is built as Meshtastic's T-Deck image is (ARDUINO_USB_MODE=0,
ARDUINO_USB_CDC_ON_BOOT=1): `Serial` is TinyUSB's CDC device on the S3's OTG
controller, the QEMU device esp-usb-otg, with the host of emu/devices/usb_otg.py
on its cable. The probe waits for the host to open the port, writes twenty
lines on it, and echoes each line typed to it. The test types one once the
probe says it is ready.

    python tools/otg_probe/build.py
    python3 -m pytest tests/test_usb_otg_probe.py -m slow
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_OTG_PROBE, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_OTG_PROBE.exists(),
                       reason="probe not built; run tools/otg_probe/build.py"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

TYPED = "hello from the host"


def _otg(outdir: Path) -> str:
    path = outdir / "t.otg.log"
    return path.read_text(errors="replace") if path.is_file() else ""


def _type_when_ready(outdir: Path) -> None:
    deadline = time.monotonic() + 120
    while "probe: ready" not in _otg(outdir):
        if time.monotonic() > deadline:
            return
        time.sleep(0.5)
    with (outdir / "input.jsonl").open("a") as fh:
        fh.write(json.dumps({"type": "serial", "port": "otg",
                             "text": TYPED + "\n"}) + "\n")
    deadline = time.monotonic() + 30
    while f"echo: {TYPED}" not in _otg(outdir) and time.monotonic() < deadline:
        time.sleep(0.5)


@pytest.fixture(scope="module")
def probed(tmp_path_factory) -> tuple[Run, Path]:
    outdir = tmp_path_factory.mktemp("otg")
    run = run_emulator(outdir, firmware=FIRMWARE_OTG_PROBE, board="tdeck",
                       timeout=180, script=_type_when_ready)
    return run, outdir


def test_the_host_opens_the_port(probed) -> None:
    run, _ = probed
    assert "probe: port open" in run.uart, run.uart[-800:]


def test_everything_the_device_writes_arrives_in_order(probed) -> None:
    _, outdir = probed
    otg = _otg(outdir)
    lines = [f"probe: line {i:02d}, long enough that a few make a packet"
             for i in range(20)]
    assert "\n".join(lines + ["probe: ready"]) in otg.replace("\r\n", "\n"), otg[-800:]


def test_what_is_typed_reaches_the_device_and_back(probed) -> None:
    run, outdir = probed
    assert f"echo: {TYPED}" in _otg(outdir), _otg(outdir)[-800:]
    assert f"probe: echoed {len(TYPED)} byte(s)" in run.uart
