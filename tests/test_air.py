#!/usr/bin/env python3
"""Two boards on the air (ADR 0036)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.air import AirMedium  # noqa: E402
from emu.devices.sx1262 import (  # noqa: E402
    IRQ_RX_DONE, IRQ_TX_DONE, MODE_RX, Sx1262,
)

CLEAR_IRQ, SET_DIO_IRQ = 0x02, 0x08
WRITE_BUFFER, READ_BUFFER = 0x0E, 0x1E
SET_TX, SET_RX = 0x83, 0x82
SET_BUFFER_BASE, SET_PACKET_PARAMS = 0x8F, 0x8C
GET_RX_BUFFER_STATUS = 0x13


def window(radio: Sx1262, frame: list[int]) -> bytes:
    reply = radio.on_segment(1, bytes(frame), True)
    radio.on_deselect()
    return reply


def listen(radio: Sx1262, watching: int = IRQ_RX_DONE) -> None:
    window(radio, [SET_DIO_IRQ, 0x00, watching, 0, 0, 0, 0, 0, 0])
    window(radio, [SET_BUFFER_BASE, 0x00, 0x00])
    window(radio, [SET_RX, 0x00, 0x00, 0x00])


def transmit(radio: Sx1262, payload: bytes) -> None:
    window(radio, [SET_BUFFER_BASE, 0x00, 0x00])
    window(radio, [WRITE_BUFFER, 0x00] + list(payload))
    window(radio, [SET_PACKET_PARAMS, 0x00, 0x08, 0x00, len(payload), 0x01, 0x00])
    window(radio, [SET_TX, 0x00, 0x00, 0x00])


@pytest.fixture()
def pair(tmp_path: Path):
    nudges: list[bool] = []
    sender = Sx1262(air=AirMedium(tmp_path, "sender"))
    receiver = Sx1262(air=AirMedium(tmp_path, "receiver"),
                      signal=nudges.append)
    yield sender, receiver, nudges
    sender.air.close()
    receiver.air.close()


def test_boards_in_the_same_directory_can_hear_each_other(pair) -> None:
    sender, receiver, _ = pair
    assert sender.air.peers() == [receiver.air.port]
    assert receiver.air.peers() == [sender.air.port]


def test_a_board_on_its_own_hears_nobody(tmp_path: Path) -> None:
    """Which is a node with nothing in range, not an error."""
    alone = Sx1262(air=AirMedium(tmp_path, "alone"))
    try:
        assert alone.air.peers() == []
        transmit(alone, b"anyone there")
        assert alone.sent == 1
    finally:
        alone.air.close()


def test_a_packet_sent_is_a_packet_received(pair) -> None:
    sender, receiver, _ = pair
    listen(receiver)
    transmit(sender, b"hello mesh")
    receiver.pump()

    assert receiver.heard == 1
    assert receiver.irq & IRQ_RX_DONE

    status = window(receiver, [GET_RX_BUFFER_STATUS, 0, 0, 0])
    assert status[2] == len(b"hello mesh"), "wrong length reported"
    payload = window(receiver, [READ_BUFFER, 0x00, 0x00] + [0] * 10)
    assert bytes(payload[3:13]) == b"hello mesh"


def test_a_board_that_is_not_listening_hears_nothing(pair) -> None:
    """The part only receives while it has been told to receive."""
    sender, receiver, _ = pair
    transmit(sender, b"into the void")
    receiver.pump()
    assert receiver.heard == 0
    assert not receiver.irq & IRQ_RX_DONE


def test_the_line_follows_the_flags(pair) -> None:
    """A host waits on the line rather than polling, so it has to go up when
    something happens and come down when the host clears it -- otherwise the
    next packet is no edge at all."""
    sender, receiver, nudges = pair
    listen(receiver)
    assert nudges and nudges[-1] is False, "the line should start low"

    transmit(sender, b"knock")
    receiver.pump()
    assert nudges[-1] is True, "nothing on the line when a packet arrived"

    window(receiver, [CLEAR_IRQ, 0xFF, 0xFF])
    assert nudges[-1] is False, "the line stayed up after the flags were cleared"


def test_transmitting_reports_that_it_finished_on_the_air(pair) -> None:
    sender, _, _ = pair
    window(sender, [SET_DIO_IRQ, 0x00, IRQ_TX_DONE, 0, 0, 0, 0, 0, 0])
    transmit(sender, b"out")
    assert sender.irq & IRQ_TX_DONE


def test_a_board_that_left_is_not_written_to(pair) -> None:
    """Its file going away is how it leaves; nothing else announces it."""
    sender, receiver, _ = pair
    receiver.air.close()
    assert sender.air.peers() == []
    transmit(sender, b"still here?")
    assert sender.sent == 1


def test_the_receiver_only_takes_what_it_is_in_range_of(pair) -> None:
    """Everyone in the directory hears everything: there is no propagation
    modelled, and this states that plainly so it is not mistaken for one."""
    sender, receiver, _ = pair
    listen(receiver)
    transmit(sender, b"a")
    receiver.pump()
    assert receiver.heard == 1
