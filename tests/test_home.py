#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Where a user's things are kept (ADR 0183): the clone, ~/.boardemul when
installed, or BOARDEMUL_HOME."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import home  # noqa: E402


def test_a_clone_keeps_them_in_the_clone(monkeypatch) -> None:
    monkeypatch.delenv("BOARDEMUL_HOME", raising=False)
    assert home.is_clone()
    assert home.home() == home.CODE_ROOT
    assert home.devices() == home.CODE_ROOT / "devices"


def test_an_installed_copy_keeps_them_in_the_user_s_home(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("BOARDEMUL_HOME", raising=False)
    monkeypatch.setattr(home, "CODE_ROOT", tmp_path / "site-packages")
    assert not home.is_clone()
    assert home.home() == Path.home() / ".boardemul"
    assert home.firmware() == Path.home() / ".boardemul" / "firmware"


def test_boardemul_home_names_another(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BOARDEMUL_HOME", str(tmp_path / "lab"))
    assert home.logs() == tmp_path / "lab" / "logs"
    assert home.builds() == tmp_path / "lab" / "build"
