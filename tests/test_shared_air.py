#!/usr/bin/env python3
"""Boards on one air keep one time (ADR 0113).

The radio is handed what the other boards sent at each barrier, and judges it
on the shared clock: these tests hand it frames directly, at guest times they
choose, and then the air medium's own barrier is exercised over loopback.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices import lora  # noqa: E402
from emu.devices.air import AirMedium  # noqa: E402
from emu.devices.sx1262 import (  # noqa: E402
    IRQ_CAD_DETECTED, IRQ_CAD_DONE, IRQ_RX_DONE, MODE_RX, MODE_STANDBY_RC,
    Sx1262, lock_symbols,
)

SET_DIO_IRQ, GET_IRQ, CLEAR_IRQ = 0x08, 0x12, 0x02
SET_RX, SET_TX, SET_STANDBY = 0x82, 0x83, 0x80
SET_CAD, SET_CAD_PARAMS = 0xC5, 0x88
SET_PACKET_PARAMS = 0x8C
MS = 1_000_000
# SF7 at 125 kHz, the model's power-on modulation: a symbol is 1.024 ms.
SYMBOL = 1_024_000


class TimedAir:
    """Stands in for an AirMedium on a timed air: keeps what is sent."""

    timed = True

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, frame: bytes) -> int:
        self.sent.append(frame)
        return 1

    def poll(self) -> list[bytes]:
        raise AssertionError("on a timed air only the barrier reads the air")


def window(radio: Sx1262, frame: list[int], at_ns: int) -> bytes:
    radio.now_ns = at_ns
    reply = radio.on_segment(1, bytes(frame), True)
    radio.on_deselect()
    return reply


def packet(start_ns: int, payload: bytes = b"hello", sender: str = "other") -> tuple[str, bytes]:
    airtime = lora.time_on_air(len(payload), 7, 0x04, 1, preamble=8)
    return sender, lora.encode(0, start_ns / 1e9, airtime, payload)


def ends(start_ns: int, payload: bytes = b"hello") -> int:
    return start_ns + int(lora.time_on_air(len(payload), 7, 0x04, 1, preamble=8) * 1e9)


@pytest.fixture()
def radio() -> Sx1262:
    radio = Sx1262(air=TimedAir())
    window(radio, [SET_DIO_IRQ, 0x00, 0x03, 0, 0x03, 0, 0, 0, 0], 0)
    window(radio, [SET_PACKET_PARAMS, 0x00, 0x08, 0x00, 5, 0x01, 0x00], 0)
    return radio


def listen(radio: Sx1262, at_ns: int) -> None:
    window(radio, [SET_RX, 0xFF, 0xFF, 0xFF], at_ns)


def test_a_packet_heard_from_its_start_is_received_at_its_end(radio: Sx1262) -> None:
    listen(radio, 0)
    assert radio.barrier(2 * MS, [packet(1 * MS)]) == (0, ends(1 * MS))
    assert radio.heard == 0
    assert radio.barrier(ends(1 * MS), []) == (1, 0)
    assert radio.heard == 1 and radio.irq & IRQ_RX_DONE


def test_a_receiver_that_comes_late_locks_on_while_preamble_remains(radio: Sx1262) -> None:
    """Twelve and a quarter symbols of preamble, eight needed: there are
    4.25 symbols to spare (ADR 0113)."""
    assert lock_symbols(7) == 8
    radio.barrier(2 * MS, [packet(1 * MS)])
    listen(radio, 1 * MS + 4 * SYMBOL)
    radio.barrier(1 * MS + 5 * SYMBOL, [])
    radio.barrier(ends(1 * MS), [])
    assert radio.heard == 1


def test_one_that_comes_too_late_was_not_listening(radio: Sx1262) -> None:
    radio.barrier(2 * MS, [packet(1 * MS)])
    listen(radio, 1 * MS + 5 * SYMBOL)
    radio.barrier(ends(1 * MS), [])
    assert radio.heard == 0 and radio.not_listening == 1


def test_a_receiver_that_leaves_part_way_loses_the_packet(radio: Sx1262) -> None:
    listen(radio, 0)
    radio.barrier(2 * MS, [packet(1 * MS)])
    window(radio, [SET_STANDBY, 0x00], 10 * MS)
    radio.barrier(ends(1 * MS), [])
    assert radio.heard == 0 and radio.cut_off == 1


def test_a_transmitting_radio_is_deaf(radio: Sx1262) -> None:
    window(radio, [SET_TX, 0, 0, 0], 0)
    radio.barrier(2 * MS, [packet(1 * MS)])
    radio.barrier(ends(1 * MS) + 100 * MS, [])
    assert radio.heard == 0 and radio.deaf == 1


def test_two_packets_on_top_of_each_other_are_both_lost(radio: Sx1262) -> None:
    listen(radio, 0)
    radio.barrier(2 * MS, [packet(1 * MS, sender="a"), packet(2 * MS, sender="b")])
    radio.barrier(3 * MS, [])
    radio.barrier(ends(2 * MS), [])
    assert radio.heard == 0 and radio.collisions == 1


def test_a_packet_that_has_not_started_waits(radio: Sx1262) -> None:
    """A board can be told of a packet before its own time reaches it."""
    listen(radio, 0)
    radio.barrier(1 * MS, [packet(3 * MS)])
    window(radio, [SET_STANDBY, 0x00], 2 * MS)
    radio.barrier(ends(3 * MS), [])
    assert radio.heard == 0 and radio.not_listening == 1


def test_a_sweep_sees_a_packet_on_the_air(radio: Sx1262) -> None:
    """6.1.5: detected if LoRa symbols were heard during it. Then STBY_RC."""
    radio.barrier(2 * MS, [packet(1 * MS)])
    window(radio, [SET_CAD_PARAMS, 0x01, 0x16, 0x0A, 0x00, 0, 0, 0], 2 * MS)
    window(radio, [SET_CAD], 2 * MS)
    radio.barrier(2 * MS + 3 * SYMBOL, [])
    flags = window(radio, [GET_IRQ, 0, 0, 0], 2 * MS + 3 * SYMBOL)
    irq = flags[2] << 8 | flags[3]
    assert irq & IRQ_CAD_DONE and irq & IRQ_CAD_DETECTED
    assert radio.mode == MODE_STANDBY_RC and radio.cad_detected == 1


def test_an_empty_channel_reads_clear(radio: Sx1262) -> None:
    window(radio, [SET_CAD_PARAMS, 0x01, 0x16, 0x0A, 0x00, 0, 0, 0], 0)
    window(radio, [SET_CAD], 0)
    radio.barrier(3 * SYMBOL, [])
    assert radio.irq & IRQ_CAD_DONE and not radio.irq & IRQ_CAD_DETECTED
    assert radio.mode == MODE_STANDBY_RC


def test_cad_rx_stays_listening_when_it_detects(radio: Sx1262) -> None:
    radio.barrier(2 * MS, [packet(1 * MS)])
    window(radio, [SET_CAD_PARAMS, 0x01, 0x16, 0x0A, 0x01, 0, 0, 0], 2 * MS)
    window(radio, [SET_CAD], 2 * MS)
    radio.barrier(2 * MS + 3 * SYMBOL, [])
    assert radio.mode == MODE_RX


def test_a_frame_counts_from_the_first_barrier_after_it_started(radio: Sx1262) -> None:
    """What a board knows at a barrier is fixed by the barrier: a frame that
    started at or after it may or may not be in hand yet, depending on how far
    the host let its sender run, so it waits for the next one (ADR 0115)."""
    listen(radio, 0)
    window(radio, [SET_CAD_PARAMS, 0x01, 0x16, 0x0A, 0x00, 0, 0, 0], 0)
    radio.barrier(1 * MS, [packet(1 * MS)])
    window(radio, [SET_CAD], 1 * MS)
    flags = window(radio, [GET_IRQ, 0, 0, 0], 1 * MS + 3 * SYMBOL)
    assert not (flags[2] << 8 | flags[3]) & IRQ_CAD_DETECTED, (
        "known before its barrier: the sweep would see it in some runs only")
    radio.barrier(1 * MS + 3 * SYMBOL, [])
    assert radio._candidates or radio._receiving or radio.not_listening, (
        "known from the next barrier on")


def test_the_detected_flag_is_bit_eight() -> None:
    """Table 8-4. It used to say 0x0040, which is CrcErr."""
    assert IRQ_CAD_DETECTED == 0x0100


def test_the_judgement_does_not_depend_on_arrival_order(radio: Sx1262) -> None:
    other = Sx1262(air=TimedAir())
    for one in (radio, other):
        listen(one, 0)
    radio.barrier(5 * MS, [packet(1 * MS, b"a", "x"), packet(400 * MS, b"b", "y")])
    other.barrier(5 * MS, [packet(400 * MS, b"b", "y"), packet(1 * MS, b"a", "x")])
    for one in (radio, other):
        one.barrier(1000 * MS, [])
    assert radio.heard == other.heard == 2


# -- the air medium's barrier -------------------------------------------------

def test_boards_meet_at_each_barrier_and_bring_their_frames(tmp_path: Path) -> None:
    one = AirMedium(tmp_path, "one", timed=True)
    two = AirMedium(tmp_path, "two", timed=True)
    listener = AirMedium(tmp_path, "listener")
    got: dict[str, list] = {}
    try:
        def board(air: AirMedium, word: bytes) -> None:
            seen = []
            for ns in (0, 1 * MS, 2 * MS):
                air.send(word + str(ns).encode())
                seen += air.barrier(ns, boards=2, give_up_s=10)
            got[air.name] = seen
        threads = [threading.Thread(target=board, args=(air, word))
                   for air, word in ((one, b"one@"), (two, b"two@"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        assert sorted(frame for _, frame in got["one"]) == [b"two@0", b"two@1000000",
                                                             b"two@2000000"]
        assert {sender for sender, _ in got["two"]} == {"one"}
        assert one.barriers == two.barriers == 3
    finally:
        for air in (one, two, listener):
            air.close()


def test_a_board_waits_for_the_others_before_its_time_starts(tmp_path: Path) -> None:
    alone = AirMedium(tmp_path, "alone", timed=True)
    try:
        alone.barrier(0, boards=2, give_up_s=0.3)
        assert alone.waited_s >= 0.3
    finally:
        alone.close()


def test_a_passive_listener_is_not_waited_for(tmp_path: Path) -> None:
    timed = AirMedium(tmp_path, "timed", timed=True)
    listener = AirMedium(tmp_path, "listener")
    try:
        timed.barrier(0, boards=1, give_up_s=5)
        assert timed.waited_s < 1
    finally:
        timed.close()
        listener.close()
