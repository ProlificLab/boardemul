#!/usr/bin/env python3
"""The access point acknowledges what is for it, and holds frames for a station
that says it is dozing (ADR 0109). Driven with frames directly, no emulator."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.tcp import ACK, SYN  # noqa: E402
from emu.devices.wifi_ap import (  # noqa: E402
    AP_ADDRESS, BROADCAST, FC1_MORE_DATA, FC1_PM, NULL_DATA, PS_POLL, AccessPoint,
    Air, Network, tim_element,
)
from tests.test_wifi_ap import (  # noqa: E402
    STATION, _connected, _is_message_one, _owing_message_one, _parse, _segment,
    _sta_frame,
)

OTHER = bytes.fromhex("0254444d0002")


def _null(pm: bool, src: bytes = STATION) -> bytes:
    """A null-data frame to the access point, ToDS, saying dozing or awake."""
    return (bytes([NULL_DATA, 0x01 | (FC1_PM if pm else 0), 0x00, 0x00])
            + AP_ADDRESS + src + AP_ADDRESS + b"\x00\x00")


def _ps_poll(aid: int = 1, src: bytes = STATION) -> bytes:
    """A PS-Poll as the device reads it back: twenty-four bytes, the last eight
    past the frame."""
    return (bytes([PS_POLL, FC1_PM]) + (0xC000 | aid).to_bytes(2, "little")
            + AP_ADDRESS + src + bytes(8))


def _held_frame(point: AccessPoint, n: int) -> Air:
    return Air(point.data_protected(STATION, b"\x08\x00", bytes([n]) * 20))


def _elements(frame: bytes) -> dict[int, bytes]:
    out, at = {}, 24 + 12
    while at + 2 <= len(frame):
        out[frame[at]] = frame[at + 2:at + 2 + frame[at + 1]]
        at += 2 + frame[at + 1]
    return out


def _dozing() -> AccessPoint:
    point = _connected()
    point.stations[STATION].aid = 1
    assert point.take(_null(True), 1, 1000) is None
    assert point.stations[STATION].dozing
    return point


# -- the TIM ------------------------------------------------------------------

def test_a_tim_with_nothing_held_is_one_octet_of_zero() -> None:
    assert tim_element(set()) == bytes([5, 4, 0, 1, 0, 0])


def test_a_tim_sets_the_bit_of_a_station_with_frames_held() -> None:
    assert tim_element({1}) == bytes([5, 4, 0, 1, 0, 0b10])


def test_a_tim_starts_at_the_largest_even_octet_before_the_first_bit() -> None:
    """AID 20 is octet 2, bit 4: the bitmap starts there, offset 2/2 = 1."""
    assert tim_element({20}) == bytes([5, 4, 0, 1, 1 << 1, 1 << 4])


def test_a_beacon_carries_a_tim_and_a_probe_response_does_not() -> None:
    point = AccessPoint(Network(ssid="x", channel=1, beacon_ms=100))
    beacon = point.tick(1, 1000).frame
    assert 5 in _elements(beacon), "every beacon carries a TIM"
    probe = (bytes([0x40, 0, 0, 0]) + BROADCAST + STATION + BROADCAST + b"\x00\x00"
             + bytes([0, 1]) + b"x")
    answer = point.take(probe, 1, 1001).frame
    assert answer[0] == 0x50 and 5 not in _elements(answer)


# -- association identifiers ----------------------------------------------------

def _join(point: AccessPoint, address: bytes) -> int:
    auth = (bytes([0xB0, 0, 0, 0]) + AP_ADDRESS + address + AP_ADDRESS + b"\x00\x00"
            + bytes([0, 0, 1, 0, 0, 0]))
    point.take(auth, 1, 1000)
    assoc = (bytes([0x00, 0, 0, 0]) + AP_ADDRESS + address + AP_ADDRESS + b"\x00\x00"
             + bytes([0x01, 0x00, 0x0A, 0x00]))
    answer = point.take(assoc, 1, 1001).frame
    return int.from_bytes(answer[28:30], "little") & 0x3FFF


def test_each_station_gets_its_own_association_identifier() -> None:
    point = AccessPoint(Network(ssid="x", channel=1, beacon_ms=10 ** 9))
    assert {_join(point, STATION), _join(point, OTHER)} == {1, 2}


# -- acknowledgement ------------------------------------------------------------

def test_a_frame_for_the_access_point_is_acknowledged() -> None:
    point = _connected()
    point.take(_null(False), 1, 1000)
    assert point.last_heard


def test_a_frame_for_someone_else_is_not() -> None:
    point = _connected()
    frame = bytearray(_null(False))
    frame[4:10] = OTHER
    point.take(bytes(frame), 1, 1000)
    assert not point.last_heard


def test_a_group_frame_wants_no_acknowledgement() -> None:
    point = _connected()
    frame = bytearray(_null(False))
    frame[4:10] = BROADCAST
    point.take(bytes(frame), 1, 1000)
    assert point.last_heard


# -- power save -----------------------------------------------------------------

def test_frames_for_a_dozing_station_are_held_and_named_in_the_tim() -> None:
    point = _dozing()
    point.outbox.append(_held_frame(point, 1))
    assert point.tick(1, 1001) is None, "nothing goes to a dozing station"
    assert point.stations[STATION].held
    point.network.beacon_ms = 0
    beacon = point.tick(1, 1002).frame
    assert _elements(beacon)[5][-1] & 0b10, "its bit is set in the TIM"


def test_waking_releases_what_was_held_in_order() -> None:
    point = _dozing()
    point.outbox += [_held_frame(point, 1), _held_frame(point, 2)]
    point.tick(1, 1001)
    first = point.take(_null(False), 1, 1002)
    assert first is not None and first.frame[-28] == 1
    assert not point.stations[STATION].held
    assert point.tick(1, 1003).frame[-28] == 2


def test_a_ps_poll_gets_one_frame_with_more_data_while_more_are_held() -> None:
    point = _dozing()
    point.outbox += [_held_frame(point, 1), _held_frame(point, 2)]
    point.tick(1, 1001)
    one = point.take(_ps_poll(), 1, 1002).frame
    assert one[1] & FC1_MORE_DATA and one[-28] == 1
    two = point.take(_ps_poll(), 1, 1003).frame
    assert not two[1] & FC1_MORE_DATA and two[-28] == 2
    empty = point.take(_ps_poll(), 1, 1004).frame
    assert empty[0] == NULL_DATA and empty[4:10] == STATION


def test_an_answer_to_a_station_that_will_doze_is_held() -> None:
    point = _connected()
    point.stations[STATION].aid = 1
    syn = bytearray(_sta_frame(_segment(7000, 8896, 1, 0, SYN)))
    syn[1] |= FC1_PM
    assert point.take(bytes(syn), 1, 1000) is None
    answer = point.take(_null(False), 1, 1001)
    assert answer is not None and _parse(answer.frame)["flags"] == SYN | ACK


def test_message_one_waits_for_a_dozing_station_to_wake() -> None:
    point = _owing_message_one()
    point.stations[STATION].aid = 1
    point.take(_null(True), 1, 900)
    assert point.tick(1, 1000) is None
    point.take(_null(False), 1, 1001)
    assert _is_message_one(point.tick(1, 1002))
