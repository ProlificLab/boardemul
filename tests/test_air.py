#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Two boards on the air (ADR 0036), on the guest's clock (ADR 0112).

Every radio here is told the guest's time by the test, as the SPI controller
tells it on a board, so nothing depends on how fast the host runs.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices import lora  # noqa: E402
from emu.devices.air import AirMedium, air_host, read_entry  # noqa: E402
from emu.devices.sx1262 import (  # noqa: E402
    IRQ_CAD_DETECTED, IRQ_CAD_DONE, IRQ_RX_DONE, IRQ_TX_DONE, MODE_FS,
    MODE_STANDBY_RC, Sx1262,
)

CLEAR_IRQ, SET_DIO_IRQ, GET_IRQ = 0x02, 0x08, 0x12
SET_CAD, SET_CAD_PARAMS = 0xC5, 0x88
WRITE_BUFFER, READ_BUFFER = 0x0E, 0x1E
SET_TX, SET_RX = 0x83, 0x82
SET_BUFFER_BASE, SET_PACKET_PARAMS = 0x8F, 0x8C
SET_RF_FREQUENCY, SET_FALLBACK = 0x86, 0x93
SET_MODULATION = 0x8B
GET_RX_BUFFER_STATUS = 0x13
# Table 8-2: STBY_RC to TX, which is when the PA has ramped and the packet
# starts (ADR 0111).
RAMP_NS = 126_000


def window(radio: Sx1262, frame: list[int]) -> bytes:
    reply = radio.on_segment(1, bytes(frame), True)
    radio.on_deselect()
    return reply


def at(radio: Sx1262, seconds: float) -> None:
    """The guest's time, as the controller would stamp the next segment."""
    radio.now_ns = int(seconds * 1e9)


def flags(radio: Sx1262) -> int:
    reply = window(radio, [GET_IRQ, 0, 0, 0])
    return reply[2] << 8 | reply[3]


def listen(radio: Sx1262, watching: int = IRQ_RX_DONE) -> None:
    window(radio, [SET_DIO_IRQ, 0x00, watching, 0x00, watching, 0, 0, 0, 0])
    window(radio, [SET_BUFFER_BASE, 0x00, 0x00])
    window(radio, [SET_RX, 0xFF, 0xFF, 0xFF])


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
    assert sender.air.peers() == [receiver.air.address]
    assert receiver.air.peers() == [sender.air.address]


def test_a_board_on_loopback_writes_only_its_port(tmp_path: Path) -> None:
    """The file every air before ADR 0145 wrote, so old and new boards read
    each other's."""
    board = AirMedium(tmp_path, "plain")
    try:
        assert (tmp_path / "plain.air").read_text() == str(board.port)
        assert read_entry("4242") == (("127.0.0.1", 4242), False)
        assert read_entry("4242 timed") == (("127.0.0.1", 4242), True)
    finally:
        board.close()


def test_a_board_elsewhere_says_where(tmp_path: Path, monkeypatch) -> None:
    """In a container on a Docker network a board is reached at its
    container's address, and its file says so (ADR 0145)."""
    monkeypatch.setenv("BOARDEMUL_AIR_HOST", "127.0.0.2")
    if not _can_bind("127.0.0.2"):
        pytest.skip("this host cannot bind 127.0.0.2")
    elsewhere = AirMedium(tmp_path, "elsewhere", timed=True)
    monkeypatch.delenv("BOARDEMUL_AIR_HOST")
    here = AirMedium(tmp_path, "here")
    try:
        assert (tmp_path / "elsewhere.air").read_text() == \
            f"{elsewhere.port} timed host=127.0.0.2"
        assert here.peers() == [("127.0.0.2", elsewhere.port)]
        assert here.send(b"across") == 1
        deadline = time.monotonic() + 2
        got = []
        while not got and time.monotonic() < deadline:
            got = elsewhere.poll_named()
        assert got == [("here", b"across")]
    finally:
        elsewhere.close()
        here.close()


def test_auto_is_the_address_the_hosts_name_resolves_to(monkeypatch) -> None:
    import socket
    monkeypatch.setenv("BOARDEMUL_AIR_HOST", "auto")
    assert air_host() == socket.gethostbyname(socket.gethostname())
    monkeypatch.delenv("BOARDEMUL_AIR_HOST")
    assert air_host() == "127.0.0.1"


def _can_bind(host: str) -> bool:
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((host, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def test_a_board_on_its_own_hears_nobody(tmp_path: Path) -> None:
    """Which is a node with nothing in range, not an error."""
    alone = Sx1262(air=AirMedium(tmp_path, "alone"))
    try:
        assert alone.air.peers() == []
        transmit(alone, b"anyone there")
        assert alone.sent == 1
    finally:
        alone.air.close()


def test_a_packet_is_received_when_its_last_bit_has_arrived(pair) -> None:
    """Not the moment it starts: RxDone waits for the whole packet, on the
    receiver's own guest clock (ADR 0112)."""
    sender, receiver, _ = pair
    listen(receiver)
    transmit(sender, b"hello mesh")
    receiver.pump()
    assert receiver.heard == 0 and not receiver.irq & IRQ_RX_DONE

    at(receiver, sender.airtime(len(b"hello mesh")))
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
    at(receiver, 1.0)
    receiver.pump()
    assert receiver.heard == 0
    assert not receiver.irq & IRQ_RX_DONE


def test_the_line_follows_the_flags(pair) -> None:
    """A host waits on the line rather than polling, so it has to go up when
    something happens and come down when the host clears it -- otherwise the
    next packet is no edge at all. The controller drives it from line()."""
    sender, receiver, nudges = pair
    listen(receiver)
    assert receiver.line() == (0, 0), "the line should start low"

    # Read once the receiver is in RX, which is when BUSY falls (8.3.1): it
    # starts where this board reads it (ADR 0112).
    at(receiver, 0.001)
    transmit(sender, b"knock")
    receiver.pump()
    ends = 1_000_000 + int(sender.airtime(len(b"knock")) * 1e9)
    assert receiver.line() == (0, ends), "the rise is due when the packet ends"

    receiver.now_ns = ends
    receiver.pump()
    assert receiver.line() == (1, 0)
    assert not nudges, "the controller was told; a second writer could only be late"

    window(receiver, [CLEAR_IRQ, 0xFF, 0xFF])
    assert receiver.line() == (0, 0), "the line stayed up after the flags were cleared"


def test_a_reception_the_controller_was_not_told_of_is_nudged(pair) -> None:
    """With no release since the packet started arriving, the controller has
    no rise scheduled, so the line is raised from this side (ADR 0112)."""
    sender, receiver, nudges = pair
    listen(receiver)
    receiver.line()
    transmit(sender, b"knock")
    receiver.pump()
    at(receiver, 1.0)
    receiver.pump()
    assert nudges == [True]


def test_transmitting_takes_the_ramp_and_the_packet(pair) -> None:
    """The packet starts when the PA has ramped and TxDone waits for its last
    bit, both on the guest's clock (13.1.4, ADR 0112). Told instantly, a host
    would key the radio again before the first packet had left."""
    sender, _, _ = pair
    window(sender, [SET_DIO_IRQ, 0x00, IRQ_TX_DONE, 0x00, IRQ_TX_DONE, 0, 0, 0, 0])
    transmit(sender, b"out")
    done = RAMP_NS + int(sender.airtime(3) * 1e9)
    assert sender.line() == (0, done)
    assert sender.airtime(3) > 0.005, "a packet takes a measurable time"

    at(sender, (done - 1) / 1e9)
    assert not flags(sender) & IRQ_TX_DONE, "a packet cannot finish early"
    at(sender, done / 1e9)
    assert flags(sender) & IRQ_TX_DONE, "the transmission never finished"


def test_the_packet_leaves_when_the_ramp_ends(pair, tmp_path: Path) -> None:
    """What other boards are told is when the first symbol went out."""
    sender, _, _ = pair
    ear = AirMedium(tmp_path, "ear")
    try:
        transmit(sender, b"x")
        sent = [lora.decode(f) for f in ear.poll()]
        assert len(sent) == 1 and sent[0].sent_at == pytest.approx(RAMP_NS / 1e9)
    finally:
        ear.close()


def test_after_its_packet_the_chip_falls_back(pair) -> None:
    """To STBY_RC unless told otherwise, and to what it was told (13.1.15)."""
    sender, _, _ = pair
    transmit(sender, b"a")
    at(sender, 1.0)
    flags(sender)
    assert sender.mode == MODE_STANDBY_RC
    window(sender, [SET_FALLBACK, 0x40])
    transmit(sender, b"b")
    at(sender, 2.0)
    flags(sender)
    assert sender.mode == MODE_FS


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
    is arithmetic rather than a guess (ADR 0037), and on the guest's clock it
    no longer depends on how quickly the host gets round to listening."""
    one = Sx1262(air=AirMedium(tmp_path, "one"))
    two = Sx1262(air=AirMedium(tmp_path, "two"))
    listener = Sx1262(air=AirMedium(tmp_path, "listener"))
    try:
        listen(listener)
        transmit(one, b"first")
        transmit(two, b"second")
        listener.pump()
        at(listener, 1.0)
        listener.pump()
        assert listener.collisions == 1, "the second packet did not collide"
        assert listener.heard == 0 and listener.received_length == 0, (
            "a collided packet was kept")
    finally:
        for radio in (one, two, listener):
            radio.air.close()


def test_one_packet_after_another_is_two_packets(tmp_path: Path) -> None:
    one = Sx1262(air=AirMedium(tmp_path, "one"))
    listener = Sx1262(air=AirMedium(tmp_path, "listener"))
    try:
        listen(listener)
        transmit(one, b"first")
        listener.pump()
        at(listener, 1.0)
        listener.pump()
        listen(listener)
        transmit(one, b"second")
        listener.pump()
        at(listener, 2.0)
        listener.pump()
        assert listener.heard == 2 and listener.collisions == 0
    finally:
        for radio in (one, listener):
            radio.air.close()


def test_a_transmitting_radio_is_deaf(pair) -> None:
    """It cannot listen while it is talking, which is why a mesh needs to take
    turns at all."""
    sender, receiver, _ = pair
    listen(receiver)
    window(receiver, [SET_MODULATION, 11, 0x05, 1, 0])
    transmit(receiver, b"talking")
    transmit(sender, b"over me")
    receiver.pump()
    # Judged once its preamble has gone by: until then a receiver that
    # entered RX could still lock on (ADR 0141).
    at(receiver, 1.0)
    receiver.pump()
    assert receiver.heard == 0 and receiver.deaf == 1


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
    at(receiver, 1.0)
    receiver.pump()
    assert receiver.heard == 1


def test_listening_before_talking_is_answered(pair) -> None:
    """A host sweeps the channel before transmitting and waits for the result.

    Saying nothing leaves it waiting there for ever, which is exactly where
    both firmwares stopped: they were not failing to transmit, they were still
    listening. Answering that the sweep finished and found nothing is what let
    Meshtastic key its radio (ADR 0036). A host that never set the sweep's
    length is answered at once.
    """
    sender, _, _ = pair
    window(sender, [SET_DIO_IRQ, 0x00, IRQ_CAD_DONE, 0x00, IRQ_CAD_DONE, 0, 0, 0, 0])
    window(sender, [SET_CAD])
    assert sender.cad_sweeps == 1
    assert sender.irq & IRQ_CAD_DONE, "the sweep never reported finishing"
    assert not sender.irq & IRQ_CAD_DETECTED, "the channel should read clear"


def test_a_sweep_lasts_its_symbols_and_half_a_symbol(pair) -> None:
    """6.1.5 and Table 13-72: four symbols at SF7 and 125 kHz, 1.024 ms
    each, then half a symbol to post-process."""
    sender, _, _ = pair
    window(sender, [SET_DIO_IRQ, 0x00, IRQ_CAD_DONE, 0x00, IRQ_CAD_DONE, 0, 0, 0, 0])
    window(sender, [SET_CAD_PARAMS, 0x02, 0x16, 0x0A, 0x00, 0, 0, 0])
    window(sender, [SET_CAD])
    done = int(4.5 * 1.024e-3 * 1e9)
    assert sender.line() == (0, done)
    at(sender, (done - 1) / 1e9)
    assert not flags(sender) & IRQ_CAD_DONE
    at(sender, done / 1e9)
    assert flags(sender) & IRQ_CAD_DONE
