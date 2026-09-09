#!/usr/bin/env python3
"""The L76K satellite receiver on the board's second serial port.

The firmware says what this part is and how it is spoken to, in its own
strings, so none of the protocol below is guessed:

    $PCAS03,0,0,0,0,0,0,0,0,0,0,,,0,0*02    "Try to init L76K . Wait stop ."
    $PCAS06,0*1B                            then it waits for $GPTXT,01,01,02
    $PCAS04,5*1C                            which constellations to use
    $PCAS03,1,0,0,0,1,0,0,0,0,0,,,0,0*02    position and recommended-minimum on
    $PCAS11,3*1E                            what kind of movement to expect

`$PCAS03` gives a rate per sentence: the first field is the fix, the fifth the
recommended minimum, and all-zero is silence. The firmware sends the silent one
first and waits for the line to go quiet, which is why a part that says nothing
at all still gets past the first step and fails the second -- the timeout the
emulator produced before this model existed.

What the part then streams is ordinary NMEA. It is generated rather than
replayed, because a position is a scenario input here as it is for the radio
(ADR 0038): a run says where the board is, and the sentences follow from it.
"""

from __future__ import annotations

import logging
import time

from emu.serial_link import SerialDevice

log = logging.getLogger("gps")

# What the firmware waits to hear back before it will believe there is a module
# on the line. It matches on this prefix, not the whole sentence.
IDENTITY = "GPTXT,01,01,02,SW=URANUS5,V5.3.0.0"
# One position report a second, which is what the part does out of the box and
# what the firmware's rate fields ask for.
FIX_INTERVAL = 1.0
# Where the fields the firmware asks for sit in a $PCAS03. The rest are other
# sentences this model does not send.
RATE_GGA = 0
RATE_RMC = 4
# A fix, rather than a receiver still looking for one. Runs are reproducible,
# so a fix that only arrives after a plausible search would only make every
# test wait for it.
SATELLITES = 9
HDOP = 0.9
GEOID_SEPARATION_M = 46.9


def checksum(body: str) -> str:
    """NMEA's own: everything between the dollar and the star, exclusive-or'd."""
    value = 0
    for char in body:
        value ^= ord(char)
    return f"{value:02X}"


def sentence(body: str) -> bytes:
    return f"${body}*{checksum(body)}\r\n".encode("ascii")


def degrees_to_nmea(value: float, hemispheres: str) -> tuple[str, str]:
    """NMEA writes an angle as whole degrees and decimal minutes, with the
    sign carried by a letter rather than by the number."""
    hemisphere = hemispheres[0] if value >= 0 else hemispheres[1]
    value = abs(value)
    degrees = int(value)
    minutes = (value - degrees) * 60.0
    width = 2 if hemispheres == "NS" else 3
    return f"{degrees:0{width}d}{minutes:07.4f}", hemisphere


class L76K(SerialDevice):
    """A satellite receiver that has a fix and reports it.

    `latitude` and `longitude` are where the board is; `epoch` is the moment
    its clock reads at the start of the run, stated so that two runs of the
    same scenario produce the same sentences.
    """

    def __init__(self, latitude: float = 0.0, longitude: float = 0.0,
                 altitude_m: float = 0.0, epoch: float | None = None):
        self.latitude = latitude
        self.longitude = longitude
        self.altitude_m = altitude_m
        # A stated instant, not the host's clock: a run that reports a
        # different time each time it is run cannot be compared with itself.
        self.epoch = 1_767_225_600.0 if epoch is None else epoch  # 2026-01-01Z
        self.streaming = {"gga": True, "rmc": True}
        self.commands: list[str] = []
        self.sentences = 0
        self._buffer = b""
        self._started = time.monotonic()
        self._next_fix = 0.0

    # -- what the guest says to it ---------------------------------------

    def receive(self, data: bytes) -> bytes:
        """Commands arrive as whole NMEA lines, so answer them line by line."""
        self._buffer += data
        answer = b""
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            answer += self._command(line.strip().decode("ascii", "replace"))
        # A line without its terminator is still a command if it is complete;
        # the firmware sends some without one.
        if self._buffer.startswith(b"$") and b"*" in self._buffer:
            line, star, rest = self._buffer.partition(b"*")
            if len(rest) >= 2:
                self._buffer = rest[2:]
                answer += self._command((line + star + rest[:2]).decode("ascii", "replace"))
        return answer

    def _command(self, line: str) -> bytes:
        if not line.startswith("$"):
            return b""
        body = line[1:].split("*")[0]
        fields = body.split(",")
        self.commands.append(fields[0])
        # What the firmware asked for, in the run's own record: a module that
        # answered but was never configured looks the same in the panel and the
        # console as one that was.
        log.info("asked for %s", body)
        if fields[0] == "PCAS03":
            rates = [f for f in fields[1:]]
            self.streaming["gga"] = _rate(rates, RATE_GGA)
            self.streaming["rmc"] = _rate(rates, RATE_RMC)
            return b""
        if fields[0] == "PCAS06":
            # The only thing the firmware waits for an answer to.
            return sentence(IDENTITY)
        # PCAS04 chooses constellations and PCAS11 the movement model. Neither
        # changes what a stationary board reports, and the firmware does not
        # wait for an answer to either.
        return b""

    # -- what it says on its own -----------------------------------------

    def poll(self) -> bytes:
        if not any(self.streaming.values()):
            return b""
        now = time.monotonic() - self._started
        if now < self._next_fix:
            return b""
        self._next_fix = now + FIX_INTERVAL
        stamp = self.epoch + now
        out = b""
        if self.streaming["gga"]:
            out += sentence(self._gga(stamp))
            self.sentences += 1
        if self.streaming["rmc"]:
            out += sentence(self._rmc(stamp))
            self.sentences += 1
        return out

    def _clock(self, stamp: float) -> tuple[str, str]:
        whole = int(stamp)
        seconds = whole % 60
        minutes = (whole // 60) % 60
        hours = (whole // 3600) % 24
        days = whole // 86400
        # Days since 1970 to a calendar date, without importing a timezone
        # database for a part that only ever reports UTC.
        year, month, day = _civil_from_days(days)
        return (f"{hours:02d}{minutes:02d}{seconds:02d}.00",
                f"{day:02d}{month:02d}{year % 100:02d}")

    def _gga(self, stamp: float) -> str:
        clock, _date = self._clock(stamp)
        lat, ns = degrees_to_nmea(self.latitude, "NS")
        lon, ew = degrees_to_nmea(self.longitude, "EW")
        return (f"GNGGA,{clock},{lat},{ns},{lon},{ew},1,{SATELLITES:02d},"
                f"{HDOP},{self.altitude_m:.1f},M,{GEOID_SEPARATION_M},M,,")

    def _rmc(self, stamp: float) -> str:
        clock, date = self._clock(stamp)
        lat, ns = degrees_to_nmea(self.latitude, "NS")
        lon, ew = degrees_to_nmea(self.longitude, "EW")
        # Standing still: no speed and so no meaningful course.
        return (f"GNRMC,{clock},A,{lat},{ns},{lon},{ew},0.00,0.00,{date},,,A")

    def summary(self) -> str:
        asked = ", ".join(sorted(set(self.commands))) or "nothing"
        return (f"L76K: {self.sentences} sentence(s) sent, asked for {asked}")


def _rate(rates: list[str], index: int) -> bool:
    if index >= len(rates):
        return False
    try:
        return int(rates[index] or 0) > 0
    except ValueError:
        return False


def _civil_from_days(days: int) -> tuple[int, int, int]:
    """Days since 1970-01-01 to a calendar date (Howard Hinnant's algorithm).

    `datetime` would do this, but it is the one place a date is needed and the
    arithmetic is shorter than the import is to justify.
    """
    days += 719468
    era = days // 146097
    doe = days - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    return year + (1 if month <= 2 else 0), month, day
