#!/usr/bin/env python3
"""Two boards on the air (ADR 0036)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.air import AirMedium  # noqa: E402
from emu.devices.sx1262 import (  # noqa: E402
    IRQ_CAD_DETECTED, IRQ_CAD_DONE, IRQ_RX_DONE, IRQ_TX_DONE, MODE_RX, Sx1262,
)

CLEAR_IRQ, SET_DIO_IRQ = 0x02, 0x08
SET_CAD = 0xC5
WRITE_BUFFER, READ_BUFFER = 0x0E, 0x1E
SET_TX, SET_RX = 0x83, 0x82
SET_BUFFER_BASE, SET_PACKET_PARAMS = 0x8F, 0x8C
SET_RF_FREQUENCY = 0x86
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


def settle(radio: Sx1262, flag: int, limit: float = 3.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        radio.pump()
        if radio.irq & flag:
            return True
        time.sleep(0.01)
    return False


def test_transmitting_takes_as_long_as_the_packet_does(pair) -> None:
    """A packet holds the channel for a time its modulation decides, and the
    flag saying it finished waits that long. Told instantly, a host would key
    the radio again before the first packet had left (ADR 0037)."""
    sender, _, _ = pair
    window(sender, [SET_DIO_IRQ, 0x00, IRQ_TX_DONE, 0, 0, 0, 0, 0, 0])
    transmit(sender, b"out")
    assert not sender.irq & IRQ_TX_DONE, "a packet cannot finish instantly"
    assert sender.airtime(3) > 0.005, "a packet takes a measurable time"
    assert settle(sender, IRQ_TX_DONE), "the transmission never finished"


def test_a_receiver_tuned_elsewhere_hears_nothing(pair) -> None:
    """Being on the same air is not being on the same channel."""
    sender, receiver, _ = pair
    window(sender, [SET_RF_FREQUENCY, 0x36, 0x40, 0x00, 0x00])
    listen(receiver)
    transmit(sender, b"on another channel")
    receiver.pump()
    assert receiver.heard == 0
    assert receiver.wrong_frequency == 1


def test_two_packets_at_once_are_lost(tmp_path: Path) -> None:
    """Two transmissions overlapping in the air arrive on top of each other
    and neither is decoded. That follows from how long each one lasts, which
    is arithmetic rather than a guess (ADR 0037)."""
    one = Sx1262(air=AirMedium(tmp_path, "one"))
    two = Sx1262(air=AirMedium(tmp_path, "two"))
    listener = Sx1262(air=AirMedium(tmp_path, "listener"))
    try:
        listen(listener)
        # A slow modulation, so both packets are still in the air together.
        for radio in (one, two):
            window(radio, [0x8B, 11, 0x05, 1, 0])       # SetModulationParams
        transmit(one, b"first")
        transmit(two, b"second")
        listener.pump()
        assert listener.collisions == 1, "the second packet did not collide"
        assert listener.received_length == 0, "a collided packet was kept"
    finally:
        for radio in (one, two, listener):
            radio.air.close()


def test_a_transmitting_radio_is_deaf(pair) -> None:
    """It cannot listen while it is talking, which is why a mesh needs to take
    turns at all."""
    sender, receiver, _ = pair
    listen(receiver)
    window(receiver, [0x8B, 11, 0x05, 1, 0])
    transmit(receiver, b"talking")
    transmit(sender, b"over me")
    receiver.pump()
    assert receiver.heard == 0


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


def test_listening_before_talking_is_answered(pair) -> None:
    """A host sweeps the channel before transmitting and waits for the result.

    Saying nothing leaves it waiting there for ever, which is exactly where
    both firmwares stopped: they were not failing to transmit, they were still
    listening. Answering that the sweep finished and found nothing is what let
    Meshtastic key its radio (ADR 0036).
    """
    sender, _, _ = pair
    window(sender, [SET_DIO_IRQ, 0x00, IRQ_CAD_DONE, 0, 0, 0, 0, 0, 0])
    window(sender, [SET_CAD])
    assert sender.cad_sweeps == 1
    assert sender.irq & IRQ_CAD_DONE, "the sweep never reported finishing"
    assert not sender.irq & IRQ_CAD_DETECTED, "the channel should read clear"
