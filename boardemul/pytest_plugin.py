# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""A pytest fixture for firmware tests (ADR 0182).

    # conftest.py
    pytest_plugins = ["boardemul.pytest_plugin"]

    def test_it_boots(boardemul_lab):
        node = boardemul_lab.device("node", build=".pio/build/t-deck")
        node.start()
        node.serial("usb").expect("Booted", timeout=120)

The lab is the page at BOARDEMUL_URL when `boardemul test` runs the tests,
or one started for the session otherwise. `boardemul_devices` are the
devices `boardemul test` started, by name.
"""

from __future__ import annotations

import os

import pytest

from boardemul.lab import Device, Lab


@pytest.fixture(scope="session")
def boardemul_lab():
    with Lab() as lab:
        yield lab


@pytest.fixture(scope="session")
def boardemul_devices(boardemul_lab) -> dict[str, Device]:
    names = [n for n in os.environ.get("BOARDEMUL_DEVICES", "").split(",") if n]
    return {name: Device(boardemul_lab, name) for name in names}
