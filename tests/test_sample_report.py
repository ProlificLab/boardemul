#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""tools/sample_report.py (ADR 0176): samples are placed by the memory map,
threads are reported busiest first, and a guess at callers is offered."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "tools" / "sample_report.py"

MAPS = """\
7f0000000000-7f0000100000 rwxp 00000000 00:00 0
7f0000200000-7f0000201000 rw-p 00000000 00:00 0
7f1000000000-7f1000010000 r-xp 00000000 00:45 17 /nowhere/qemu-system-xtensa
"""


def report(tmp_path: Path, samples: str, *args: str) -> str:
    path = tmp_path / "samples.txt"
    path.write_text(samples)
    (tmp_path / "samples.txt.maps").write_text(MAPS)
    return subprocess.run([sys.executable, str(REPORT), str(path), *args],
                          capture_output=True, text=True, check=True).stdout


def test_generated_code_is_told_from_the_rest(tmp_path) -> None:
    out = report(tmp_path, "7 7f0000000010\n7 7f0000000020\n7 7f0000200010\n"
                           "9 7f0000000030\n")
    lines = out.splitlines()
    assert lines[0] == "=== thread 7: 3 samples", "the busiest thread first"
    assert "66.7%  [generated code]" in lines[1]
    assert "33.3%  [anon rw-p]" in lines[2]
    assert lines[3] == "=== thread 9: 1 samples"


def test_an_address_outside_every_mapping_says_so(tmp_path) -> None:
    assert "[unmapped]" in report(tmp_path, "7 10\n")


def test_callers_come_from_the_stack_words_inside_qemu(tmp_path) -> None:
    # Two samples in generated code, with words above the stack pointer: one
    # in QEMU's text and one in generated code, which is not a QEMU frame.
    samples = ("7 7f0000000010 7f0000000040 7f1000000100\n"
               "7 7f0000000010 7f1000000100\n")
    out = report(tmp_path, samples, "10", "--callers", "generated")
    assert out.splitlines()[0] == "=== first QEMU function above 'generated': 2 samples"
    assert "100.0%  qemu-system-xtensa:?" in out, "the binary has no symbols to name it by"
