#!/usr/bin/env python3
"""The satellite receiver on the second serial port (ADR 0052).

Everything asserted here is something the firmware's own strings ask for: the
commands it sends, the reply it waits for, and the two sentences it turns back
on. Nothing checks NMEA against a specification the firmware does not read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices.gps import IDENTITY, L76K, checksum, degrees_to_nmea  # noqa: E402

# The firmware's own handshake, copied from its strings.
STOP = b"$PCAS03,0,0,0,0,0,0,0,0,0,0,,,0,0*02\r\n"
QUERY = b"$PCAS06,0*1B\r\n"
CONSTELLATIONS = b"$PCAS04,5*1C\r\n"
START = b"$PCAS03,1,0,0,0,1,0,0,0,0,0,,,0,0*02\r\n"
NAVIGATION = b"$PCAS11,3*1E\r\n"


def lines(data: bytes) -> list[str]:
    return [line for line in data.decode("ascii").split("\r\n") if line]


@pytest.fixture
def module() -> L76K:
    # Somewhere with a sign on both axes, so a mistake in either shows up.
    return L76K(latitude=51.5074, longitude=-0.1278, altitude_m=35.0)


def test_it_answers_the_question_the_firmware_waits_for(module) -> None:
    """The only command whose answer the firmware waits for. Without it the
    run prints 'Get L76K timeout!' and decides there is no module."""
    reply = module.receive(QUERY).decode("ascii")
    assert reply.startswith("$" + IDENTITY.split(",SW=")[0]), reply
    assert "$GPTXT,01,01,02" in reply, reply


def test_being_told_to_stop_makes_it_stop(module) -> None:
    """The firmware's first move, and it then waits for the line to go quiet."""
    module.receive(STOP)
    module._next_fix = 0.0
    assert module.poll() == b""


def test_being_told_to_start_turns_both_sentences_on(module) -> None:
    """The rate fields name the fix and the recommended minimum; those are the
    two the firmware reads."""
    module.receive(STOP)
    module.receive(START)
    module._next_fix = 0.0
    kinds = {line.split(",")[0] for line in lines(module.poll())}
    assert kinds == {"$GNGGA", "$GNRMC"}, kinds


def test_every_sentence_carries_a_good_checksum(module) -> None:
    """A receiver that got this wrong would be discarded silently, which is
    the kind of fault that costs a day."""
    module._next_fix = 0.0
    for line in lines(module.poll()) + lines(module.receive(QUERY)):
        body, star, given = line[1:].partition("*")
        assert star, line
        assert checksum(body) == given, line


def test_the_position_reported_is_the_one_it_was_given(module) -> None:
    """Degrees and decimal minutes, with the sign carried by a letter."""
    module._next_fix = 0.0
    gga = next(line for line in lines(module.poll()) if line.startswith("$GNGGA"))
    fields = gga.split(",")
    assert (fields[2], fields[3]) == ("5130.4440", "N"), fields
    assert (fields[4], fields[5]) == ("00007.6680", "W"), fields
    assert fields[9] == "35.0", fields          # the altitude it was given


def test_an_angle_south_and_east_flips_both_letters() -> None:
    """The other half of every sign, which a single position cannot show."""
    assert degrees_to_nmea(-33.8688, "NS") == ("3352.1280", "S")
    assert degrees_to_nmea(151.2093, "EW") == ("15112.5580", "E")


def test_it_says_it_has_a_fix(module) -> None:
    """A receiver still searching reports a quality of zero and the firmware
    keeps waiting; this one is placed, so it has a fix."""
    module._next_fix = 0.0
    out = lines(module.poll())
    gga = next(line for line in out if line.startswith("$GNGGA"))
    rmc = next(line for line in out if line.startswith("$GNRMC"))
    assert gga.split(",")[6] == "1", gga        # fix quality
    assert rmc.split(",")[2] == "A", rmc        # the reading is valid


def test_two_runs_of_the_same_scenario_report_the_same_time(module) -> None:
    """The clock is a stated instant, not the host's: a run that reported a
    different time each time could not be compared with itself."""
    other = L76K(latitude=51.5074, longitude=-0.1278, altitude_m=35.0)
    module._next_fix = other._next_fix = 0.0
    module._started = other._started = 0.0
    assert lines(module.poll())[0].split(",")[1] == lines(other.poll())[0].split(",")[1]


def test_the_whole_handshake_leaves_it_streaming(module) -> None:
    """The five commands in the order the firmware sends them."""
    for command in (STOP, QUERY, CONSTELLATIONS, START, NAVIGATION):
        module.receive(command)
    module._next_fix = 0.0
    assert len(lines(module.poll())) == 2
    assert set(module.commands) == {"PCAS03", "PCAS04", "PCAS06", "PCAS11"}


def test_a_command_arriving_in_pieces_is_still_one_command(module) -> None:
    """A serial line has no notion of a message, so a command can arrive split
    across reads and the model must not lose it."""
    assert module.receive(QUERY[:6]) == b""
    reply = module.receive(QUERY[6:])
    assert b"GPTXT" in reply, reply
