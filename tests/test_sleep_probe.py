#!/usr/bin/env python3
"""Light sleep wakes on its timer and on a pin (ADR 0165).

The probe light-sleeps two seconds on the RTC timer, then until GPIO 0, the
T-Deck's trackball press, is low, and says each time what woke it by
esp_sleep_get_wakeup_cause()'s numbering. The test presses the trackball
once the probe says it is waiting for it.

    python tools/sleep_probe/build.py
    python3 -m pytest tests/test_sleep_probe.py -m slow
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_SLEEP_PROBE, Run, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_SLEEP_PROBE.exists(),
                       reason="probe not built; run tools/sleep_probe/build.py"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

WAKEUP_TIMER, WAKEUP_GPIO = 4, 7      # esp_sleep_source_t, ESP-IDF 4.4


def _uart(outdir: Path) -> str:
    path = outdir / "t.uart.log"
    return path.read_text(errors="replace") if path.is_file() else ""


def _press_when_asked(outdir: Path) -> None:
    """Press the trackball once the probe sleeps on it, as a person would."""
    deadline = time.monotonic() + 120
    while "sleeping until GPIO 0 is low" not in _uart(outdir):
        if time.monotonic() > deadline:
            return
        time.sleep(0.5)
    time.sleep(1)                       # let it be asleep, not about to be
    with (outdir / "input.jsonl").open("a") as fh:
        fh.write(json.dumps({"type": "trackball", "press": True}) + "\n")
    time.sleep(1)
    with (outdir / "input.jsonl").open("a") as fh:
        fh.write(json.dumps({"type": "trackball", "press": False}) + "\n")
    deadline = time.monotonic() + 30
    while "probe: done" not in _uart(outdir) and time.monotonic() < deadline:
        time.sleep(0.5)


@pytest.fixture(scope="module")
def slept(tmp_path_factory) -> tuple[Run, Path]:
    outdir = tmp_path_factory.mktemp("sleep")
    run = run_emulator(outdir, firmware=FIRMWARE_SLEEP_PROBE, board="tdeck",
                       timeout=180, script=_press_when_asked)
    return run, outdir


def test_the_timer_wakes_it_when_asked(slept) -> None:
    _run, outdir = slept
    uart = _uart(outdir)
    found = re.search(r"probe: woke, err (-?\d+), cause (\d+), after (\d+) ms", uart)
    assert found, uart[-800:]
    err, cause, after = (int(g) for g in found.groups())
    assert err == 0 and cause == WAKEUP_TIMER, uart[-800:]
    # Two seconds asked for; ESP-IDF's own sleep accounting adds a little.
    assert 2000 <= after < 3000, after


def test_the_trackball_wakes_it_from_a_pin_sleep(slept) -> None:
    _run, outdir = slept
    uart = _uart(outdir)
    woken = re.findall(r"probe: woke, err (-?\d+), cause (\d+)\n", uart)
    assert woken == [("0", str(WAKEUP_GPIO))], uart[-800:]
    assert "probe: done" in uart
