#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The runner must never block QEMU on its own stderr.

A pipe nobody drains fills at 64 KB and then stops QEMU on the next write,
which looks exactly like a guest stall. This fake QEMU says a megabyte and
exits; with a pipe it would hang until the hard timeout.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.run import run_qemu  # noqa: E402


def test_a_chatty_stderr_is_kept_and_does_not_stall(tmp_path: Path) -> None:
    fake = [sys.executable, "-c",
            "import sys; sys.stderr.write('x' * (1 << 20)); sys.stderr.flush()"]
    uart, err = tmp_path / "uart.log", tmp_path / "stderr.log"
    reason, wall = run_qemu(fake, uart, err, timeout=60, stall=60)
    assert reason == "guest exited (code 0)"
    assert err.stat().st_size == 1 << 20
    assert wall < 30
