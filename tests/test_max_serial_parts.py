#!/usr/bin/env python3
"""The T-Deck Max's serial parts and their ports (ADR 0105): a modem on the
second UART, a u-blox receiver on the third. Fast: models and command lines.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import board  # noqa: E402
from emu.devices.gps import UBloxM10, checksum, ubx  # noqa: E402
from emu.devices.modem import A7682E  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now: float | None = None

    def __call__(self) -> float | None:
        return self.now


# -- the receiver -------------------------------------------------------------

def _lines(data: bytes) -> list[str]:
    return [line.split("*")[0] for line in data.decode("ascii").split("\r\n") if line]


def test_the_receiver_streams_once_a_guest_second() -> None:
    clock = _Clock()
    gnss = UBloxM10(clock=clock)
    assert gnss.poll() == b"", "nothing before the board's clock has started"
    clock.now = 0.5
    assert gnss.poll() == b"", "the first fix is a second after power-on"
    clock.now = 1.0
    first = gnss.poll()
    assert first.count(b"$GN") == 4
    clock.now = 1.99
    assert gnss.poll() == b"", "no second fix within the same guest second"
    clock.now = 2.0
    assert gnss.poll()


def test_it_streams_what_is_on_by_default_in_message_id_order() -> None:
    """GGA, GLL, RMC and VTG are on out of the box (CFG-MSGOUT, Table 69);
    GSA and GSV are too, but need a sky (ADR 0117)."""
    clock = _Clock()
    gnss = UBloxM10(latitude=48.8584, longitude=2.2945, clock=clock)
    clock.now = 1.0
    kinds = [line.split(",")[0] for line in _lines(gnss.poll())]
    assert kinds == ["$GNGGA", "$GNGLL", "$GNRMC", "$GNVTG"], kinds


def test_indoors_it_streams_empty_fields() -> None:
    """No fix and no time: "it will output empty fields" (2.5.6), whose
    example is GLL's."""
    clock = _Clock()
    gnss = UBloxM10(clock=clock)
    clock.now = 1.0
    assert _lines(gnss.poll()) == ["$GNGGA,,,,,,0,00,99.99,,,,,,", "$GNGLL,,,,,,V,N",
                                   "$GNRMC,,V,,,,,,,,,,N,V", "$GNVTG,,,,,,,,,N"]


def test_given_a_position_it_has_a_fix_there() -> None:
    """Five decimals of a minute and two of a second, as the interface
    description writes them; standing still, the speed is 0.000 and the course
    empty, as in its receiver output in 2.5.3."""
    clock = _Clock()
    gnss = UBloxM10(latitude=48.8584, longitude=2.2945, altitude_m=35.0, clock=clock)
    clock.now = 1.0
    gga, gll, rmc, vtg = _lines(gnss.poll())
    assert gga == "$GNGGA,000001.00,4851.50400,N,00217.67000,E,1,09,0.90,35.0,M,46.9,M,,"
    assert gll == "$GNGLL,4851.50400,N,00217.67000,E,000001.00,A,A"
    assert rmc == "$GNRMC,000001.00,A,4851.50400,N,00217.67000,E,0.000,,010126,,,A,V"
    assert vtg == "$GNVTG,,T,,M,0.000,N,0.000,K,A"


def test_a_report_is_stamped_with_its_own_instant() -> None:
    """Not with whenever it was asked, so the stamps are the same every run."""
    early, late = _Clock(), _Clock()
    early.now, late.now = 3.01, 3.98
    one = UBloxM10(latitude=1.0, longitude=1.0, clock=early).poll()
    two = UBloxM10(latitude=1.0, longitude=1.0, clock=late).poll()
    assert one == two and b"000003.00" in one


def test_the_interface_descriptions_gll_example_comes_out() -> None:
    """2.5.6's valid fix, at the place and instant it was taken. Its talker is
    GP, a GPS-only receiver's; this one combines constellations, so GN."""
    clock = _Clock()
    epoch = 1_767_225_600.0 + 12 * 3600 + 49 * 60 + 23 - 1   # 12:49:23 on its day
    gnss = UBloxM10(latitude=47 + 17.11634 / 60, longitude=8 + 33.91297 / 60,
                    epoch=epoch, clock=clock)
    clock.now = 1.0
    gll = next(line for line in _lines(gnss.poll()) if "GLL" in line)
    assert gll == "$GNGLL,4717.11634,N,00833.91297,E,124923.00,A,A"
    assert checksum("GPGLL,4717.11634,N,00833.91297,E,124923.00,A,A") == "6E"


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
