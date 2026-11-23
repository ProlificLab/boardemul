#!/usr/bin/env python3
"""Which clock a run's guest keeps (ADR 0178): instructions unless the host's
is asked for, and never both at once."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "emu/run.py", *args], cwd=REPO,
                          capture_output=True, text=True, timeout=60)


def test_both_clocks_at_once_is_refused() -> None:
    done = run("missing.bin", "--icount", "--host-clock")
    assert done.returncode != 0
    assert "two different clocks" in done.stderr + done.stdout


def test_the_host_clock_is_an_option_the_help_names() -> None:
    done = run("--help")
    assert "--host-clock" in done.stdout
    assert "the default since ADR 0178" in done.stdout.replace("\n", " ").replace("  ", " ")
