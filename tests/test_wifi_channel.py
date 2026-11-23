#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The access point is on one channel, and heard and hearing only there
(ADR 0110). Driven with frames directly, no emulator."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.wifi_ap import AP_ADDRESS, BROADCAST, AccessPoint, Network  # noqa: E402
from tests.test_wifi_power_save import _elements  # noqa: E402

STATION = bytes.fromhex("0254444d0001")
AP_CHANNEL = 6


def _point() -> AccessPoint:
    return AccessPoint(Network(ssid="x", channel=AP_CHANNEL, beacon_ms=100))


def _probe() -> bytes:
    return (bytes([0x40, 0, 0, 0]) + BROADCAST + STATION + BROADCAST + b"\x00\x00"
            + bytes([0, 1]) + b"x")


def _auth() -> bytes:
    return (bytes([0xB0, 0, 0, 0]) + AP_ADDRESS + STATION + AP_ADDRESS + b"\x00\x00"
            + bytes([0, 0, 1, 0, 0, 0]))


def test_a_probe_on_its_channel_is_answered_naming_that_channel() -> None:
    answer = _point().take(_probe(), AP_CHANNEL, 1000)
    assert answer is not None and _elements(answer.frame)[3] == bytes([AP_CHANNEL])


def test_a_probe_on_another_channel_is_not_heard() -> None:
    point = _point()
    assert point.take(_probe(), 1, 1000) is None
    assert point.last_heard, "a group frame wants no acknowledgement anyway"
    assert not point.stations, "it heard nobody"


def test_a_unicast_frame_on_another_channel_is_unacknowledged_and_changes_nothing() -> None:
    point = _point()
    assert point.take(_auth(), 11, 1000) is None
    assert not point.last_heard
    assert STATION not in point.stations


def test_the_same_frame_on_its_channel_is_answered() -> None:
    point = _point()
    assert point.take(_auth(), AP_CHANNEL, 1000) is not None
    assert point.last_heard and point.stations[STATION].authenticated


def test_a_beacon_names_its_own_channel_wherever_the_station_is_tuned() -> None:
    beacon = _point().tick(1, 1000).frame
    assert _elements(beacon)[3] == bytes([AP_CHANNEL])
