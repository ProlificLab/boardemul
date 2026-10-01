#!/usr/bin/env python3
"""The T-Deck Max's serial parts and their ports (ADR 0105): a modem on the
second UART, a u-blox receiver on the third. Fast: models and command lines.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import board  # noqa: E402
from emu.devices.gps import UBloxM10, ubx  # noqa: E402
from emu.devices.modem import A7682E  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now: float | None = None

    def __call__(self) -> float | None:
        return self.now


# -- the receiver -------------------------------------------------------------

def test_the_receiver_streams_once_a_guest_second() -> None:
    clock = _Clock()
    gnss = UBloxM10(clock=clock)
    assert gnss.poll() == b"", "nothing before the board's clock has started"
    clock.now = 10.0
    assert gnss.poll() == b"", "the first fix is a second after power-on"
    clock.now = 10.5
    assert gnss.poll() == b""
    clock.now = 11.0
    first = gnss.poll()
    assert first.count(b"$GN") == 2
    assert gnss.poll() == b"", "no second fix within the same guest second"


def test_indoors_it_streams_with_no_fix() -> None:
    clock = _Clock()
    gnss = UBloxM10(clock=clock)
    clock.now = 0.0
    gnss.poll()
    clock.now = 1.0
    out = gnss.poll()
    assert b"$GNRMC,,V," in out and b"$GNGGA,,,,,,0," in out


def test_given_a_position_it_has_a_fix_there() -> None:
    clock = _Clock()
    gnss = UBloxM10(latitude=48.8584, longitude=2.2945, clock=clock)
    clock.now = 0.0
    gnss.poll()
    clock.now = 1.0
    out = gnss.poll()
    assert b",A,4851.5040,N,00217.6700,E," in out


def test_configuration_is_acknowledged() -> None:
    cfg_cfg = ubx(0x06, 0x09, bytes(12))
    answer = UBloxM10().receive(cfg_cfg)
    assert answer == ubx(0x05, 0x01, bytes([0x06, 0x09]))


def test_a_poll_of_the_rate_is_answered_with_the_rate_then_acknowledged() -> None:
    """The factory demo's check after CFG-RATE waits for the 0x06/0x08 frame."""
    answer = UBloxM10().receive(bytes.fromhex("b562060800000e30"))
    rate = ubx(0x06, 0x08, bytes.fromhex("e80301000100"))
    assert answer == rate + ubx(0x05, 0x01, bytes([0x06, 0x08]))


def test_a_frame_split_across_reads_is_still_one_frame() -> None:
    gnss = UBloxM10()
    frame = ubx(0x06, 0x09, bytes(12))
    assert gnss.receive(frame[:5]) == b""
    assert gnss.receive(frame[5:]) == ubx(0x05, 0x01, bytes([0x06, 0x09]))


# -- the modem ----------------------------------------------------------------

def test_the_modem_answers_at_with_its_echo() -> None:
    assert A7682E().receive(b"AT\r") == b"AT\r\r\nOK\r\n"


def test_echo_can_be_turned_off() -> None:
    modem = A7682E()
    modem.receive(b"ATE0\r")
    assert modem.receive(b"AT+CLIP=1\r") == b"\r\nOK\r\n"


def test_an_unmodelled_command_is_an_error_and_is_recorded() -> None:
    modem = A7682E()
    modem.receive(b"ATE0\r")
    assert modem.receive(b"AT+CSQ\r") == b"\r\nERROR\r\n"
    assert modem.unmodelled == ["AT+CSQ"]


# -- the ports ----------------------------------------------------------------

def test_the_max_puts_its_modem_on_the_second_uart_and_its_receiver_on_the_third(tmp_path) -> None:
    hw = board.build(tmp_path)
    args = hw.qemu_args()
    serials = [args[i + 1] for i, a in enumerate(args) if a == "-serial"]
    assert serials == [f"unix:{hw.modem.socket_path}", f"unix:{hw.gps.socket_path}"]
    assert any(a.startswith("esp-guest-clock,socket=") for a in args)
