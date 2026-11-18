#!/usr/bin/env python3
"""A person can type into both serial ports (ADR 0156).

The probe answers each line typed into UART0 or the USB Serial/JTAG port.
The test types a line into each through the run's input file, as the page
does, and reads the answers in each port's log.

    python tools/serial_probe/build.py
    python3 -m pytest tests/test_serial_probe.py -m slow
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_SERIAL_PROBE, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_SERIAL_PROBE.exists(),
                       reason="probe not built; run tools/serial_probe/build.py"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def _log(outdir: Path, port: str) -> str:
    path = outdir / f"t.{port}.log"
    return path.read_text(errors="replace") if path.is_file() else ""


def _type(outdir: Path) -> None:
    """Wait for the probe to listen on both ports, then type a line to each."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if ("probe: uart listening" in _log(outdir, "uart")
                and "probe: usb listening" in _log(outdir, "usb")):
            break
        time.sleep(0.5)
    with (outdir / "input.jsonl").open("a") as fh:
        for port, text in (("uart", "hello uart"), ("usb", "hello usb")):
            fh.write(json.dumps({"type": "serial", "port": port,
                                 "text": text + "\n"}) + "\n")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ("heard: hello uart" in _log(outdir, "uart")
                and "heard: hello usb" in _log(outdir, "usb")):
            return
        time.sleep(0.5)


@pytest.fixture(scope="module", params=["tdeck", "tdeck-max"])
def probed(request, tmp_path_factory) -> tuple[Run, Path]:
    outdir = tmp_path_factory.mktemp("serial")
    run = run_emulator(outdir, firmware=FIRMWARE_SERIAL_PROBE, board=request.param,
                       timeout=120, script=_type)
    return run, outdir


def test_a_line_typed_into_the_uart_reaches_the_firmware(probed) -> None:
    _run, outdir = probed
    assert "probe: uart heard: hello uart" in _log(outdir, "uart"), _log(outdir, "uart")[-800:]


def test_a_line_typed_into_the_usb_port_reaches_the_firmware(probed) -> None:
    _run, outdir = probed
    assert "probe: usb heard: hello usb" in _log(outdir, "usb"), _log(outdir, "usb")[-800:]
