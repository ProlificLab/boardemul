#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The WiFi example, end to end (ADRs 0182, 0185): a firmware PlatformIO
built -- its firmware.bin the application alone -- joins the scenario's
network and answers HTTP through the forwarded port.

Slow, and it needs the example built first:

    cd examples/wifi-http && pio run
    python3 -m pytest tests/test_example_wifi_http.py -m slow
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import REPO_ROOT, qemu_available  # noqa: E402

EXAMPLE = REPO_ROOT / "examples" / "wifi-http"

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (EXAMPLE / ".pio" / "build" / "t-deck" / "firmware.bin").exists(),
                       reason="the example is not built (pio run in examples/wifi-http)"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]


def test_the_example_joins_its_network_and_serves_its_status(tmp_path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "boardemul", "scenario", "check.toml",
         "--logs", str(tmp_path / "logs")],
        cwd=EXAMPLE, capture_output=True, text=True, timeout=1200,
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)))
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-2000:])
    assert "ok   step 2, web: http /status" in done.stdout
