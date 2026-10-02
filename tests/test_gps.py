#!/usr/bin/env python3
"""The satellite receiver on the second serial port (ADRs 0052, 0116).

What the firmware asks for is in its own strings: the commands it sends, the
reply it waits for, and the two sentences it turns back on. What the part says
unprompted, and in what form, is Quectel's L76K GNSS Protocol Specification,
whose example sentences are reproduced here to the checksum.
"""

from __future__ import annotations

import calendar
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices import gps  # noqa: E402
from emu.devices.gps import IDENTITY, L76K, checksum, degrees_to_nmea  # noqa: E402

# The firmware's own handshake, copied from its strings.
STOP = b"$PCAS03,0,0,0,0,0,0,0,0,0,0,,,0,0*02\r\n"
QUERY = b"$PCAS06,0*1B\r\n"
CONSTELLATIONS = b"$PCAS04,5*1C\r\n"
START = b"$PCAS03,1,0,0,0,1,0,0,0,0,0,,,0,0*02\r\n"
NAVIGATION = b"$PCAS11,3*1E\r\n"
# Where the specification's examples were taken: 3149.29103 N, 11706.92916 E.
EXAMPLE_PLACE = (31 + 49.29103 / 60, 117 + 6.92916 / 60)


def lines(data: bytes) -> list[str]:
    return [line for line in data.decode("ascii").split("\r\n") if line]


class _Clock:
    """The board's clock, in seconds since power-on; None before it is heard."""

    def __init__(self, now: float | None = 0.0) -> None:
        self.now = now

    def __call__(self) -> float | None:
        return self.now


def placed(clock: _Clock) -> L76K:
    # Somewhere with a sign on both axes, so a mistake in either shows up.
    return L76K(latitude=51.5074, longitude=-0.1278, altitude_m=35.0, clock=clock)


def at(when: str, clock: _Clock, **where) -> L76K:
    """A module whose first fix, one second after power-on, is at `when` UTC."""
    epoch = calendar.timegm(tuple(map(int, when.split()))) - 1
    return L76K(epoch=epoch, clock=clock, **where)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def module(clock) -> L76K:
    """A module the firmware has configured, which is the state it is in for
    all but the first moments of a run."""
    part = placed(clock)
    part.receive(STOP)
    part.receive(START)
    return part


def next_fix(part: L76K, clock: _Clock) -> list[str]:
    clock.now = (clock.now or 0.0) + 1.0
    return lines(part.poll())


def test_it_answers_the_question_the_firmware_waits_for(module) -> None:
    """The only command whose answer the firmware waits for. Without it the
    run prints 'Get L76K timeout!' and decides there is no module."""
    reply = module.receive(QUERY).decode("ascii")
    assert reply.startswith("$" + IDENTITY.split(",SW=")[0]), reply
    assert "$GPTXT,01,01,02" in reply, reply


def test_it_talks_from_power_on(clock) -> None:
    """Unaddressed, it streams what the specification's Table 17 says is on
    out of the box -- of that, every sentence a position and a time decide --
    in the order a $PCAS03 lists them (ADR 0116)."""
    kinds = [line.split(",")[0] for line in next_fix(placed(clock), clock)]
    assert kinds == ["$GNGGA", "$GNGLL", "$GNRMC", "$GNVTG", "$GNZDA"], kinds


def test_its_first_report_comes_a_second_after_power_on(clock) -> None:
    part = placed(clock)
    for now in (0.0, 0.5, 0.99):
        clock.now = now
        assert part.poll() == b"", now
    clock.now = 1.0
    assert part.poll()


def test_it_says_nothing_before_the_boards_clock_is_heard() -> None:
    assert placed(_Clock(None)).poll() == b""


def test_it_reports_once_a_second_of_the_boards_time(clock) -> None:
    """By the board's clock, not the host's: a slow guest is not flooded, which
    is what once kept this model silent until spoken to (ADR 0052)."""
    part = placed(clock)
    reports = 0
    for tick in range(1, 501):            # five seconds in 10 ms ticks
        clock.now = tick / 100
        reports += bool(part.poll())
    assert reports == 5


def test_a_reader_that_stalled_gets_the_latest_fix_not_a_backlog(clock) -> None:
    part = placed(clock)
    next_fix(part, clock)
    clock.now = 5.3
    out = lines(part.poll())
    assert len(out) == 5, out
    assert out[0].split(",")[1] == "000005.000", out[0]
    assert part.poll() == b""


def test_being_told_to_stop_makes_it_stop(module, clock) -> None:
    """The firmware's first move, and it then waits for the line to go quiet."""
    module.receive(STOP)
    assert next_fix(module, clock) == []


def test_being_told_to_start_turns_both_sentences_on(module, clock) -> None:
    """The rate fields name the fix and the recommended minimum; those are the
    two the firmware reads."""
    module.receive(STOP)
    module.receive(START)
    kinds = {line.split(",")[0] for line in next_fix(module, clock)}
    assert kinds == {"$GNGGA", "$GNRMC"}, kinds


def test_an_empty_rate_keeps_the_one_already_set(clock) -> None:
    """"Null means keeping the previous configuration" (2.3.3)."""
    part = placed(clock)
    part.receive(gps.sentence("PCAS03,,,,,0,,,,,,,,0,0"))
    kinds = [line.split(",")[0] for line in next_fix(part, clock)]
    assert kinds == ["$GNGGA", "$GNGLL", "$GNVTG", "$GNZDA"], kinds


def test_a_rate_of_n_is_once_every_n_fixes(clock) -> None:
    part = placed(clock)
    part.receive(gps.sentence("PCAS03,2,0,0,0,1,0,0,0,0,0,,,0,0"))
    gga = ["$GNGGA" in "".join(next_fix(part, clock)) for _ in range(4)]
    assert gga == [False, True, False, True], gga


def test_a_rate_for_what_it_cannot_send_is_held_but_sends_nothing(clock) -> None:
    """It has no sky for GSA and GSV and no antenna for ANT (ADR 0116)."""
    part = placed(clock)
    part.receive(gps.sentence("PCAS03,0,0,1,1,0,0,0,1,0,0,,,0,0"))
    assert part.rates["gsa"] == part.rates["gsv"] == part.rates["ant"] == 1
    assert next_fix(part, clock) == []


def test_the_specifications_examples_come_out_to_the_checksum(clock) -> None:
    """RMC, GLL and VTG from 2.2.1, 2.2.6 and 2.2.5, at the place and the
    instant they were taken."""
    part = at("2020 4 25 7 15 56", clock, latitude=EXAMPLE_PLACE[0],
              longitude=EXAMPLE_PLACE[1])
    out = next_fix(part, clock)
    assert "$GNRMC,071556.000,A,3149.29103,N,11706.92916,E,0.00,0.00,250420,,,A,V*09" in out
    assert "$GNGLL,3149.29103,N,11706.92916,E,071556.000,A,A*45" in out
    assert "$GNVTG,0.00,T,,M,0.00,N,0.00,K,A*23" in out


def test_the_time_and_date_come_out_as_the_specification_writes_them(clock) -> None:
    """ZDA from 2.2.8, whose example is another day."""
    out = next_fix(at("2021 10 21 5 37 12", clock), clock)
    assert "$GNZDA,053712.000,21,10,2021,00,00*49" in out, out


def test_the_fix_comes_out_in_the_specifications_form(clock, monkeypatch) -> None:
    """GGA from 2.2.2. Its satellites, HDOP and geoid separation are the
    example's, not this model's stated ones; the form is the point."""
    monkeypatch.setattr(gps, "SATELLITES", 21)
    monkeypatch.setattr(gps, "HDOP", 0.7)
    monkeypatch.setattr(gps, "GEOID_SEPARATION_M", -5.0)
    part = at("2020 4 25 7 15 56", clock, latitude=EXAMPLE_PLACE[0],
              longitude=EXAMPLE_PLACE[1], altitude_m=75.7)
    out = next_fix(part, clock)
    assert "$GNGGA,071556.000,3149.29103,N,11706.92916,E,1,21,0.7,75.7,M,-5.0,M,,*69" in out


def test_every_sentence_carries_a_good_checksum(clock) -> None:
    """A receiver that got this wrong would be discarded silently, which is
    the kind of fault that costs a day."""
    part = placed(clock)
    for line in next_fix(part, clock) + lines(part.receive(QUERY)):
        body, star, given = line[1:].partition("*")
        assert star, line
        assert checksum(body) == given, line


def test_the_position_reported_is_the_one_it_was_given(module, clock) -> None:
    """Degrees and decimal minutes, five places, with the sign carried by a
    letter."""
    gga = next(line for line in next_fix(module, clock) if line.startswith("$GNGGA"))
    fields = gga.split(",")
    assert (fields[2], fields[3]) == ("5130.44400", "N"), fields
    assert (fields[4], fields[5]) == ("00007.66800", "W"), fields
    assert fields[9] == "35.0", fields          # the altitude it was given


def test_an_angle_south_and_east_flips_both_letters() -> None:
    """The other half of every sign, which a single position cannot show."""
    assert degrees_to_nmea(-33.8688, "NS") == ("3352.12800", "S")
    assert degrees_to_nmea(151.2093, "EW") == ("15112.55800", "E")


def test_it_says_it_has_a_fix(module, clock) -> None:
    """A receiver still searching reports a quality of zero and the firmware
    keeps waiting; this one is placed, so it has a fix."""
    out = next_fix(module, clock)
    gga = next(line for line in out if line.startswith("$GNGGA"))
    rmc = next(line for line in out if line.startswith("$GNRMC"))
    assert gga.split(",")[6] == "1", gga        # fix quality
    assert rmc.split(",")[2] == "A", rmc        # the reading is valid


def test_a_report_is_stamped_with_its_own_instant(clock) -> None:
    """Not with whenever it was asked: two modules polled at different moments
    within the same second say the same thing, so a run can be compared with
    itself."""
    early, late = _Clock(2.01), _Clock(2.97)
    assert lines(placed(early).poll()) == lines(placed(late).poll())
    assert lines(placed(early).poll())[0].split(",")[1] == "000002.000"


def test_the_whole_handshake_leaves_it_streaming(module, clock) -> None:
    """The five commands in the order the firmware sends them."""
    for command in (STOP, QUERY, CONSTELLATIONS, START, NAVIGATION):
        module.receive(command)
    assert len(next_fix(module, clock)) == 2
    assert set(module.commands) == {"PCAS03", "PCAS04", "PCAS06", "PCAS11"}


def test_a_command_arriving_in_pieces_is_still_one_command(module) -> None:
    """A serial line has no notion of a message, so a command can arrive split
    across reads and the model must not lose it."""
    assert module.receive(QUERY[:6]) == b""
    reply = module.receive(QUERY[6:])
    assert b"GPTXT" in reply, reply
