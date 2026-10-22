#!/usr/bin/env python3
"""The teardown watchdog: a hang leaves stacks and dies (ADR 0136).

Fast, via a subprocess — the watchdog's exit is os._exit, which has no
business inside the test process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_a_hanging_teardown_leaves_its_stacks_and_dies(tmp_path: Path) -> None:
    log = tmp_path / "teardown.log"
    harness = f"""
import sys, threading, faulthandler
sys.path.insert(0, r"{REPO_ROOT}")
from emu import run
run.TEARDOWN_WATCHDOG_SECONDS = 0.4
run.arm_teardown_watchdog(r"{tmp_path}")
threading.Event().wait()      # the stop that never finishes
"""
    proc = subprocess.run([sys.executable, "-c", harness], capture_output=True,
                          timeout=30)
    assert proc.returncode != 0, "a hung teardown did not exit"
    stacks = log.read_text(errors="replace")
    assert "Thread" in stacks
    assert "wait" in stacks and "threading.py" in stacks    # named where it sticks


def test_a_quiet_teardown_cancels_the_watchdog(tmp_path: Path) -> None:
    harness = f"""
import sys, faulthandler
sys.path.insert(0, r"{REPO_ROOT}")
from emu import run
run.TEARDOWN_WATCHDOG_SECONDS = 0.4
run.arm_teardown_watchdog(r"{tmp_path}")
faulthandler.cancel_dump_traceback_later()   # what main does when it finishes
import time
time.sleep(1)                                # longer than the watchdog
print("survived")
"""
    proc = subprocess.run([sys.executable, "-c", harness], capture_output=True,
                          timeout=30)
    assert proc.returncode == 0 and b"survived" in proc.stdout
    # The log's opening is eager; the autopsy is what the cancel prevents.
    assert not (tmp_path / "teardown.log").read_text()
