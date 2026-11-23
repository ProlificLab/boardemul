#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""How long the radio says each command keeps it busy (ADR 0111).

Every number here is the datasheet's -- Semtech DS.SX1261-2.W.APP, Rev. 1.2 --
and where it gives none the radio owes none. The SPI controller holds the BUSY
pin for what the radio answers; these tests ask the radio directly, and then
ask it over the wire the controller uses.
"""

from __future__ import annotations

import socket
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.sx1262 import (  # noqa: E402
    CALIBRATE, GET_IRQ_STATUS, MODE_FS, MODE_STANDBY_RC, READ_REGISTER,
    SET_DIO3_AS_TCXO, SET_FS, SET_RF_FREQUENCY, SET_RX, SET_SLEEP,
    SET_STANDBY, SET_TX, Sx1262, VERSION_ADDRESS, VERSION_STRING,
    WRITE_REGISTER,
)
from emu.spi_link import (  # noqa: E402
    ANSWER, BUSY_HELD, LINE_LEFT, OFFER, SEG_DUPLEX, SEG_RELEASE,
    SEG_RELEASE_BUSY, SEG_RESET, SEG_SKIP, SEG_WRITE, SEGMENT, SpiServer,
)

US = 1000


def window(radio: Sx1262, frame: list[int]) -> tuple[int, int]:
    """One chip-select window, and what BUSY owes for it."""
    radio.on_segment(1, bytes(frame), True)
    radio.on_deselect()
    return radio.busy()


def with_tcxo(radio: Sx1262, steps: int = 320) -> None:
    """RadioLib's default: 5 ms, which is 320 steps of 15.625 us."""
    window(radio, [SET_DIO3_AS_TCXO, 0x00, steps >> 16, (steps >> 8) & 0xFF,
                   steps & 0xFF])


@pytest.fixture()
def radio() -> Sx1262:
    return Sx1262()


def test_a_read_leaves_busy_low(radio: Sx1262) -> None:
    """8.3.1: a read is handled without the state machine."""
    assert window(radio, [READ_REGISTER, 0x03, 0x20, 0x00, 0, 0]) == (0, 0)
    assert window(radio, [GET_IRQ_STATUS, 0, 0, 0]) == (0, 0)


def test_a_write_with_no_published_time_owes_none(radio: Sx1262) -> None:
    assert window(radio, [WRITE_REGISTER, 0x07, 0x40, 0x01]) == (0, 0)
    assert window(radio, [SET_RF_FREQUENCY, 0x36, 0x40, 0x00, 0x00]) == (0, 0)


def test_mode_switches_take_table_8_2s_times(radio: Sx1262) -> None:
    assert window(radio, [SET_STANDBY, 0x01]) == (31 * US, 0)      # RC -> XOSC
    assert window(radio, [SET_FS]) == (40 * US, 0)                 # XOSC -> FS
    assert window(radio, [SET_RX, 0, 0, 0]) == (41 * US, 0)        # FS -> RX
    assert window(radio, [SET_TX, 0, 0, 0]) == (92 * US, 0)        # RX -> TX


def test_a_switch_the_table_does_not_list_owes_nothing(radio: Sx1262) -> None:
    window(radio, [SET_RX, 0, 0, 0])
    assert window(radio, [SET_STANDBY, 0x00]) == (0, 0)            # RX -> RC


def test_a_tcxo_adds_its_delay_to_a_switch_out_of_rc(radio: Sx1262) -> None:
    """13.3.6: the clock is off in STBY_RC, and a mode that needs it waits for
    it. RadioLib asks for 5 ms."""
    with_tcxo(radio)
    assert window(radio, [SET_RX, 0, 0, 0]) == ((83 + 5000) * US, 0)
    window(radio, [SET_STANDBY, 0x01])                    # RX -> XOSC, unlisted
    window(radio, [SET_STANDBY, 0x00])
    assert window(radio, [SET_STANDBY, 0x01]) == ((31 + 5000) * US, 0)
    assert window(radio, [SET_TX, 0, 0, 0]) == (105 * US, 0), (
        "the clock is already running out of XOSC")


def test_calibrating_takes_three_and_a_half_milliseconds(radio: Sx1262) -> None:
    assert window(radio, [CALIBRATE, 0x7F]) == (3500 * US, 0)


def test_sleep_holds_busy_until_the_select_wakes_it(radio: Sx1262) -> None:
    """13.1.1 and Table 8-2: high for the whole of sleep, then 340 us for a
    warm start or 3500 us for a cold one after NSS falls."""
    assert window(radio, [SET_SLEEP, 0x04]) == (BUSY_HELD, 340 * US)
    assert window(radio, [0x00]) == (0, 0), "the waking NOP owes nothing more"
    assert window(radio, [SET_SLEEP, 0x00]) == (BUSY_HELD, 3500 * US)


def test_a_cold_start_forgets_and_a_warm_one_does_not(radio: Sx1262) -> None:
    window(radio, [SET_RF_FREQUENCY, 0x36, 0x40, 0x00, 0x00])
    window(radio, [SET_SLEEP, 0x04])
    window(radio, [0x00])
    assert radio.frequency == 0x36400000
    window(radio, [SET_SLEEP, 0x00])
    window(radio, [0x00])
    assert radio.frequency == 0


def test_reset_holds_busy_then_starts_up(radio: Sx1262) -> None:
    """Table 8-3: BUSY is high in reset. Start-up runs the power-on
    calibration, which is a cold start's 3500 us."""
    radio.on_reset(True)
    assert radio.busy() == (BUSY_HELD, BUSY_HELD)
    radio.on_reset(False)
    assert radio.busy() == (3500 * US, 0)


def test_reset_returns_the_chip_to_its_reset_state(radio: Sx1262) -> None:
    with_tcxo(radio)
    window(radio, [SET_RF_FREQUENCY, 0x36, 0x40, 0x00, 0x00])
    window(radio, [WRITE_REGISTER, 0x07, 0x40, 0xAB])
    window(radio, [SET_FS])
    radio.on_reset(True)
    radio.on_reset(False)
    radio.busy()
    assert radio.mode == MODE_STANDBY_RC
    assert radio.frequency == 0 and radio.registers[0x0740] == 0
    assert bytes(radio.registers[VERSION_ADDRESS:VERSION_ADDRESS + 6]) == VERSION_STRING
    assert window(radio, [SET_RX, 0, 0, 0]) == (83 * US, 0), (
        "4.1.4: only a reset takes the chip back off its TCXO")


def test_a_command_sent_into_a_reset_is_not_taken(radio: Sx1262) -> None:
    radio.on_reset(True)
    window(radio, [SET_FS])
    assert radio.mode == MODE_STANDBY_RC


def test_what_is_owed_is_collected_once(radio: Sx1262) -> None:
    window(radio, [CALIBRATE, 0x7F])
    assert radio.busy() == (0, 0)


def test_the_mode_after_a_switch_is_the_one_asked_for(radio: Sx1262) -> None:
    window(radio, [SET_FS])
    assert radio.mode == MODE_FS


# -- over the wire ----------------------------------------------------------

needs_unix = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                                reason="the controller's socket is a Unix one")


class _Controller:
    """Stands in for esp_spi_master.c: sends headers, reads what comes back."""

    def __init__(self, path: Path) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(str(path))

    def send(self, kind: int, cs: int, data: bytes = b"", dc: int = 0,
             at_ns: int = 0, length: int | None = None) -> None:
        """A segment; `length` stands for the data's own when a segment
        counts bytes it does not carry, as a skip does."""
        count = len(data) if length is None else length
        self.sock.sendall(SEGMENT.pack(kind, dc, cs, 0, count, at_ns) + data)

    def read(self, count: int) -> bytes:
        buf = b""
        while len(buf) < count:
            buf += self.sock.recv(count - len(buf))
        return buf

    def reply(self, count: int) -> bytes:
        """A duplex answer, and the offer after it (ADR 0172)."""
        data = self.read(count)
        (offered,) = OFFER.unpack(self.read(OFFER.size))
        if offered:
            self.read(offered)
        return data

    def answer(self) -> tuple[int, int, int, int]:
        return ANSWER.unpack(self.read(ANSWER.size))

    def busy(self) -> tuple[int, int]:
        return self.answer()[:2]


@pytest.fixture()
def wired(tmp_path: Path):
    radio = Sx1262()
    server = SpiServer(tmp_path / "spi.sock", {1: radio})
    server.start()
    controller = _Controller(tmp_path / "spi.sock")
    yield controller, radio
    controller.sock.close()
    server.stop()


@needs_unix
def test_a_release_on_a_busy_select_is_answered(wired) -> None:
    controller, _ = wired
    controller.send(SEG_DUPLEX, 1, bytes([CALIBRATE, 0x7F]))
    controller.reply(2)
    controller.send(SEG_RELEASE_BUSY, 1)
    assert controller.busy() == (3500 * US, 0)


@needs_unix
def test_a_reset_edge_is_answered_with_its_level(wired) -> None:
    controller, radio = wired
    controller.send(SEG_RESET, 1, dc=0)
    assert controller.busy() == (BUSY_HELD, BUSY_HELD)
    assert radio.in_reset
    controller.send(SEG_RESET, 1, dc=1)
    assert controller.busy() == (3500 * US, 0)
    assert not radio.in_reset


@needs_unix
def test_a_select_with_nothing_on_it_is_not_busy(wired) -> None:
    controller, _ = wired
    controller.send(SEG_RELEASE_BUSY, 2)
    assert controller.busy() == (0, 0)


@needs_unix
def test_a_plain_release_still_goes_unanswered(wired) -> None:
    """A select without a BUSY line keeps the one-way release, so the next
    thing read back is the next duplex reply and not a stray answer."""
    controller, radio = wired
    controller.send(SEG_DUPLEX, 1, bytes([CALIBRATE, 0x7F]))
    controller.reply(2)
    controller.send(SEG_RELEASE, 1)
    controller.send(SEG_DUPLEX, 1, bytes([GET_IRQ_STATUS, 0, 0, 0]))
    assert controller.reply(4)[0] == radio.status


@needs_unix
def test_a_part_that_raises_still_answers(tmp_path: Path) -> None:
    class Broken(Sx1262):
        def busy(self) -> tuple[int, int]:
            raise RuntimeError("model bug")

    server = SpiServer(tmp_path / "spi.sock", {1: Broken()})
    server.start()
    controller = _Controller(tmp_path / "spi.sock")
    try:
        controller.send(SEG_RELEASE_BUSY, 1)
        assert controller.busy() == (0, 0), "the guest must not wedge on a model bug"
    finally:
        controller.sock.close()
        server.stop()


@needs_unix
def test_a_segment_says_when_the_guest_sent_it(wired) -> None:
    """ADR 0112: the radio learns the guest's time from the bus itself."""
    controller, radio = wired
    controller.send(SEG_DUPLEX, 1, bytes([GET_IRQ_STATUS, 0, 0, 0]), at_ns=123_456)
    controller.reply(4)
    assert radio.now_ns == 123_456


@needs_unix
def test_the_answer_carries_the_interrupt_line(wired) -> None:
    """A transmission's TxDone, routed to DIO1, is a rise the controller can
    schedule: the ramp from STBY_RC to TX (126 us) after the release, and with
    nothing to send, no airtime after that (ADR 0112). TxDone is enabled and
    routed to DIO1, the two masks SetDioIrqParams keeps apart (ADR 0141)."""
    controller, _ = wired
    controller.send(SEG_DUPLEX, 1, bytes([0x08, 0x00, 0x01, 0x00, 0x01, 0, 0, 0, 0]))
    controller.reply(9)
    controller.send(SEG_RELEASE_BUSY, 1)
    assert controller.answer()[2:] == (0, 0), "nothing is due yet"
    controller.send(SEG_DUPLEX, 1, bytes([0x83, 0, 0, 0]), at_ns=1_000_000)
    controller.reply(4)
    controller.send(SEG_RELEASE_BUSY, 1, at_ns=1_000_000)
    assert controller.answer() == (126 * US, 0, 0, 1_000_000 + 126 * US)


@needs_unix
def test_an_empty_select_leaves_the_line_alone(wired) -> None:
    controller, _ = wired
    controller.send(SEG_RELEASE_BUSY, 2)
    assert controller.answer()[2] == LINE_LEFT


def test_the_answer_fits_what_the_controller_reads() -> None:
    """As esp_spi_master.c's EspSpiBusyReply: two words, the line's level,
    three bytes of padding, and the guest time of its next rise."""
    assert ANSWER.size == 20
    assert ANSWER.pack(1, 2, 1, 3) == struct.pack("<IIB3xQ", 1, 2, 1, 3)
    assert SEGMENT.size == 16, "esp_spi_master.c's EspSpiSegment"


@needs_unix
def test_bytes_answered_from_an_offer_reach_the_part(tmp_path: Path) -> None:
    """ADR 0172: a duplex answer carries what the part will answer to idle
    bytes; the controller answers those itself and then says how many it
    used, and the part ends where it would have been byte by byte."""
    from emu import sdimage
    from emu.devices.sdcard import DATA_TOKEN, IDLE, SdCard
    from tests.test_sdcard import initialise

    card = SdCard(sdimage.build(size_mb=8))
    initialise(card)
    block = bytes([0x55]) * 512
    card.image[5 * 512:6 * 512] = block
    idle = bytes([IDLE])
    server = SpiServer(tmp_path / "spi.sock", {0: card})
    server.start()
    controller = _Controller(tmp_path / "spi.sock")
    try:
        controller.send(SEG_WRITE, 0, bytes([0x40 | 17, 0, 0, 0, 5, 0x01]), dc=1)
        controller.send(SEG_DUPLEX, 0, idle, dc=1)
        controller.read(1)
        (offered,) = OFFER.unpack(controller.read(OFFER.size))
        offer = controller.read(offered)
        assert DATA_TOKEN in offer and block in offer
        controller.send(SEG_SKIP, 0, dc=1, length=offered)
        controller.send(SEG_DUPLEX, 0, idle, dc=1)
        assert controller.reply(1) == bytes([IDLE]), "the block was taken as read"
        assert card.blocks_read == 1 and not card.out
    finally:
        controller.sock.close()
        server.stop()
