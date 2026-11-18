#!/usr/bin/env python3
"""The radio says a packet is arriving, and a sweep leaves RX (ADR 0141).

Each test is one of the defects re-derived on 2026-11-18 from the datasheet
(SX1261/2 Rev. 1.2), RadioLib and Meshtastic, which made `test_mesh` lose
packets under load: flags logged and routed as SetDioIrqParams says, a packet
announced before its end, a sweep that returns the chip to STBY_RC on either
air, and a judge that does not credit a radio that had left RX.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices import lora  # noqa: E402
from emu.devices.air import AirMedium  # noqa: E402
from emu.devices.sx1262 import (  # noqa: E402
    HEADER_SYMBOLS, IRQ_CAD_DETECTED, IRQ_CAD_DONE, IRQ_HEADER_VALID,
    IRQ_PREAMBLE_DETECTED, IRQ_RX_DONE, IRQ_TX_DONE, MODE_STANDBY_RC,
    SYNC_SYMBOLS, Sx1262, lock_symbols,
)

SET_DIO_IRQ, GET_IRQ, CLEAR_IRQ = 0x08, 0x12, 0x02
SET_RX, SET_TX, SET_CAD, SET_CAD_PARAMS = 0x82, 0x83, 0xC5, 0x88
SET_PACKET_PARAMS, SET_BUFFER_BASE = 0x8C, 0x8F
WRITE_BUFFER = 0x0E
MS = 1_000_000
SYMBOL = 1_024_000                      # SF7 at 125 kHz, the power-on modulation
# What Meshtastic's receive asks for (SX126xInterface::startReceive, seen on
# the wire as `08 0276 0002`): RxDone, PreambleDetected, HeaderValid,
# HeaderErr, CrcErr and Timeout logged; RxDone alone on DIO1.
MESHTASTIC_RX = [SET_DIO_IRQ, 0x02, 0x76, 0x00, 0x02, 0, 0, 0, 0]


class TimedAir:
    timed = True

    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def send(self, frame: bytes) -> int:
        self.sent.append(frame)
        return 1


def window(radio: Sx1262, frame: list[int], at_ns: int | None = None) -> bytes:
    if at_ns is not None:
        radio.now_ns = at_ns
    reply = radio.on_segment(1, bytes(frame), True)
    radio.on_deselect()
    return reply


def flags(radio: Sx1262) -> int:
    reply = window(radio, [GET_IRQ, 0, 0, 0])
    return reply[2] << 8 | reply[3]


def packet(start_ns: int, payload: bytes = b"hello") -> tuple[str, bytes]:
    airtime = lora.time_on_air(len(payload), 7, 0x04, 1, preamble=8)
    return "other", lora.encode(0, start_ns / 1e9, airtime, payload)


def ends(start_ns: int, payload: bytes = b"hello") -> int:
    return start_ns + int(lora.time_on_air(len(payload), 7, 0x04, 1, preamble=8) * 1e9)


def timed_radio(implicit: bool = False) -> Sx1262:
    radio = Sx1262(air=TimedAir())
    window(radio, MESHTASTIC_RX, 0)
    window(radio, [SET_PACKET_PARAMS, 0x00, 0x08, 0x01 if implicit else 0x00, 5, 0x01, 0x00], 0)
    return radio


# -- 1. logged and routed --------------------------------------------------


def test_a_flag_the_host_did_not_enable_is_not_logged() -> None:
    """13.3.2: by default every IRQ is masked."""
    radio = Sx1262()
    window(radio, [SET_TX, 0, 0, 0], 0)
    radio.now_ns = MS
    assert not flags(radio) & IRQ_TX_DONE


def test_a_flag_enabled_but_not_routed_is_logged_and_leaves_dio1_low() -> None:
    """13.3.2.1: a DIO is set if both its mask and IrqMask have the bit."""
    radio = Sx1262()
    window(radio, [SET_DIO_IRQ, 0x00, 0x01, 0x00, 0x00, 0, 0, 0, 0], 0)
    window(radio, [SET_TX, 0, 0, 0], 0)
    assert radio.line() == (0, 0), "nothing routed, so no rise is due"
    radio.now_ns = MS
    assert flags(radio) & IRQ_TX_DONE
    assert radio.line() == (0, 0)


# -- 2. a packet announced before its end ----------------------------------


def test_a_reception_announces_its_preamble_then_its_header_then_its_end() -> None:
    radio = timed_radio()
    window(radio, [SET_RX, 0xFF, 0xFF, 0xFF], 0)
    radio.barrier(2 * MS, [packet(1 * MS)])
    preamble = 1 * MS + lock_symbols(7) * SYMBOL
    header = 1 * MS + int((8 + SYNC_SYMBOLS + HEADER_SYMBOLS) * SYMBOL)
    assert radio.line() == (0, ends(1 * MS)), "DIO1 waits for RxDone alone"

    radio.barrier(preamble - 1, [])
    assert not radio.irq & IRQ_PREAMBLE_DETECTED
    radio.barrier(preamble, [])
    assert radio.irq & IRQ_PREAMBLE_DETECTED and not radio.irq & IRQ_HEADER_VALID
    assert radio.line() == (0, ends(1 * MS)), "a preamble does not raise DIO1"
    radio.barrier(header, [])
    assert radio.irq & IRQ_HEADER_VALID and not radio.irq & IRQ_RX_DONE
    radio.barrier(ends(1 * MS), [])
    assert radio.irq & IRQ_RX_DONE and radio.line() == (1, 0)


def test_a_packet_without_a_header_never_says_it_has_one() -> None:
    radio = timed_radio(implicit=True)
    window(radio, [SET_RX, 0xFF, 0xFF, 0xFF], 0)
    radio.barrier(2 * MS, [packet(1 * MS)])
    radio.barrier(ends(1 * MS), [])
    assert radio.irq & IRQ_RX_DONE and not radio.irq & IRQ_HEADER_VALID


def test_leaving_rx_mid_packet_cancels_what_it_had_not_yet_said() -> None:
    radio = timed_radio()
    window(radio, [SET_RX, 0xFF, 0xFF, 0xFF], 0)
    radio.barrier(2 * MS, [packet(1 * MS)])
    preamble = 1 * MS + lock_symbols(7) * SYMBOL
    radio.barrier(preamble, [])
    window(radio, [0x80, 0x00], preamble + 1)               # SetStandby
    radio.barrier(ends(1 * MS) + MS, [])
    assert radio.irq & IRQ_PREAMBLE_DETECTED, "what was logged stays logged"
    assert not radio.irq & (IRQ_HEADER_VALID | IRQ_RX_DONE)
    assert radio.cut_off == 1 and radio.heard == 0


# -- 4. not credited after leaving RX --------------------------------------


def test_a_radio_that_left_rx_before_the_packet_was_known_is_not_credited() -> None:
    """In RX and locked on, then transmitting, all before the barrier that
    brought the packet: it lost the packet when it left (ADR 0141)."""
    radio = timed_radio()
    window(radio, [SET_RX, 0xFF, 0xFF, 0xFF], 0)
    window(radio, [SET_TX, 0, 0, 0], 12 * MS)
    radio.barrier(20 * MS, [packet(1 * MS)])
    radio.barrier(ends(1 * MS) + MS, [])
    assert radio.heard == 0 and radio.cut_off == 1


def test_an_instant_in_rx_before_transmitting_is_deafness_not_a_reception() -> None:
    """Locking takes symbols; a radio that went straight on to transmit never
    locked on."""
    radio = timed_radio()
    window(radio, [SET_RX, 0xFF, 0xFF, 0xFF], 0)
    window(radio, [SET_TX, 0, 0, 0], 1 * MS + 10_000)
    radio.barrier(2 * MS, [packet(1 * MS)])
    radio.barrier(ends(1 * MS) + MS, [])
    assert radio.heard == 0 and radio.deaf == 1 and radio.cut_off == 0


# -- 3. the sweep on the wall-clock air ------------------------------------


def _node(tmp_path: Path, name: str) -> Sx1262:
    radio = Sx1262(air=AirMedium(tmp_path, name))
    window(radio, [SET_DIO_IRQ, 0x03, 0xF7, 0x03, 0xF7, 0, 0, 0, 0], 0)
    window(radio, [SET_BUFFER_BASE, 0x00, 0x00], 0)
    return radio


def _transmit(radio: Sx1262, payload: bytes, at_ns: int) -> None:
    window(radio, [WRITE_BUFFER, 0x00] + list(payload), at_ns)
    window(radio, [SET_PACKET_PARAMS, 0x00, 0x08, 0x00, len(payload), 0x01, 0x00], at_ns)
    window(radio, [SET_TX, 0, 0, 0], at_ns)


def test_a_clear_sweep_on_the_wall_clock_air_returns_to_standby(tmp_path: Path) -> None:
    """13.4.7, on this air too: ADR 0111's exception is closed."""
    node = _node(tmp_path, "node")
    try:
        window(node, [SET_CAD_PARAMS, 0x02, 0x16, 0x0A, 0x00, 0, 0, 0], 0)
        window(node, [SET_CAD], 0)
        node.now_ns = 5 * SYMBOL
        node.pump()
        assert node.irq & IRQ_CAD_DONE and not node.irq & IRQ_CAD_DETECTED
        assert node.mode == MODE_STANDBY_RC
    finally:
        node.air.close()


def test_a_packet_after_the_sweep_does_not_overwrite_what_the_node_sends(
        tmp_path: Path) -> None:
    """What happened on the air on 2026-10-03: left in RX after its sweep, a
    node received the other's packet into the buffer it had just written, and
    transmitted that. Back in standby it does not receive it at all."""
    node, other = _node(tmp_path, "node"), _node(tmp_path, "other")
    ear = AirMedium(tmp_path, "ear")
    try:
        window(node, [SET_CAD_PARAMS, 0x02, 0x16, 0x0A, 0x00, 0, 0, 0], 0)
        window(node, [SET_CAD], 0)
        node.now_ns = 5 * SYMBOL
        node.pump()                                     # the sweep ends, clear
        _transmit(other, b"thei", 0)                    # the other node speaks
        window(node, [WRITE_BUFFER, 0x00] + list(b"mine"), 6 * SYMBOL)
        node.now_ns = 7 * SYMBOL
        node.pump()                                     # its packet is read now,
        # and ends before this node, slowed by a loaded host, keys its radio.
        later = 7 * SYMBOL + int(lora.time_on_air(4, 7, 0x04, 1, preamble=8) * 1e9) + MS
        node.now_ns = later
        node.pump()
        window(node, [SET_PACKET_PARAMS, 0x00, 0x08, 0x00, 4, 0x01, 0x00], later)
        window(node, [SET_TX, 0, 0, 0], later)
        sent = {name: lora.decode(frame).payload for name, frame in ear.poll_named()}
        assert sent == {"node": b"mine", "other": b"thei"}
        assert node.heard == 0
    finally:
        for radio in (node, other):
            radio.air.close()
        ear.close()


def test_a_sweep_on_the_wall_clock_air_sees_a_packet_on_the_air(tmp_path: Path) -> None:
    node, other = _node(tmp_path, "node"), _node(tmp_path, "other")
    try:
        window(node, [SET_CAD_PARAMS, 0x02, 0x16, 0x0A, 0x00, 0, 0, 0], 0)
        window(node, [SET_CAD], 0)
        _transmit(other, b"busy", 0)
        node.now_ns = 1 * SYMBOL
        node.pump()                                     # read during the sweep
        node.now_ns = 5 * SYMBOL
        node.pump()
        assert node.irq & IRQ_CAD_DETECTED and node.cad_detected == 1
        assert node.heard == 0
    finally:
        for radio in (node, other):
            radio.air.close()


def test_a_listener_knows_who_sent_each_frame(tmp_path: Path) -> None:
    """By the board, not by an address inside the packet (ADR 0141)."""
    one, two = _node(tmp_path, "one"), _node(tmp_path, "two")
    ear = AirMedium(tmp_path, "ear")
    try:
        _transmit(one, b"a", 0)
        _transmit(two, b"b", 0)
        assert sorted(name for name, _frame in ear.poll_named()) == ["one", "two"]
    finally:
        for radio in (one, two):
            radio.air.close()
        ear.close()
