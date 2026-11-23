#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Every source file says its licence (ADR 0179): GPL-2.0-or-later in
qemu-patch/, which is compiled into QEMU, Apache-2.0 everywhere else."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True,
                             text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return out.split()


def sources() -> list[tuple[str, str]]:
    found = []
    for name in tracked():
        if name.startswith(("third_party/", "logs/", "firmware/")):
            continue
        suffix = Path(name).suffix
        if name.startswith("qemu-patch/") and suffix in (".c", ".h"):
            found.append((name, "GPL-2.0-or-later"))
        elif suffix in (".py", ".sh", ".c", ".h"):
            found.append((name, "Apache-2.0"))
    return found


def test_every_source_file_names_its_licence() -> None:
    wrong = []
    for name, licence in sources():
        head = (REPO / name).read_bytes()[:600].decode("utf-8", "replace")
        if f"SPDX-License-Identifier: {licence}" not in head:
            wrong.append(f"{name}: wants {licence}")
    assert not wrong, "\n".join(wrong)


def test_both_licence_texts_are_there() -> None:
    assert "Apache License" in (REPO / "LICENSE").read_text()
    assert "GNU GENERAL PUBLIC LICENSE" in (REPO / "qemu-patch" / "LICENSE").read_text()
    assert "Prolific Lab" in (REPO / "NOTICE").read_text()
