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

What the part streams is ordinary NMEA, from power-on and once a second of the
board's time. Which sentences, and in what form, is Quectel's *L76K GNSS
Protocol Specification* V1.1 (ADR 0116). They are generated rather than
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
# The fields of a $PCAS03, in order (the specification's 2.3.3). Each is a
# rate: a sentence once every N fixes, or never for 0.
PCAS03_FIELDS = ("gga", "gll", "gsa", "gsv", "rmc", "vtg", "zda", "ant")
# The ones sent, in that order. GSA and GSV describe a sky and ANT an antenna,
# neither of which this model has (ADR 0116).
SENT = ("gga", "gll", "rmc", "vtg", "zda")
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
    sign carried by a letter rather than by the number. Both parts here write
    five decimals of a minute, as their documents do (ADRs 0116, 0117)."""
    hemisphere = hemispheres[0] if value >= 0 else hemispheres[1]
    value = abs(value)
    degrees = int(value)
    minutes = (value - degrees) * 60.0
    width = 2 if hemispheres == "NS" else 3
    return f"{degrees:0{width}d}{minutes:08.5f}", hemisphere


class L76K(SerialDevice):
    """A satellite receiver that has a fix and reports it.

    `latitude` and `longitude` are where the board is; `epoch` is the moment
    its clock reads at the start of the run, stated so that two runs of the
    same scenario produce the same sentences. `clock` is the board's (ADR
    0104), which it reports by; without one it keeps the host's, which only a
    test with no board needs.
    """

    def __init__(self, latitude: float = 0.0, longitude: float = 0.0,
                 altitude_m: float = 0.0, epoch: float | None = None,
                 clock=None):
        self.latitude = latitude
        self.longitude = longitude
        self.altitude_m = altitude_m
        # A stated instant, not the host's clock: a run that reports a
        # different time each time it is run cannot be compared with itself.
        self.epoch = 1_767_225_600.0 if epoch is None else epoch  # 2026-01-01Z
        # Out of the box every sentence is on, once a fix (the specification's
        # Table 17). The ones this model cannot send keep their rates too, so
        # what was set is what is held.
        self.rates = dict.fromkeys(PCAS03_FIELDS, 1)
        # It talks from power-on, as the part does. By the board's clock that
        # is a report a second for the firmware to drain when it silences the
        # module; by the host's it was a flood on a slow guest, which is why
        # this model once waited to be spoken to (ADRs 0052, 0116).
        self._clock = clock
        self._started = time.monotonic()
        self._fixes = 0           # the last fix reported, in the board's seconds
        self.commands: list[str] = []
        self.sentences = 0
        self._buffer = b""

    def _now(self) -> float | None:
        if self._clock is None:
            return time.monotonic() - self._started
        return self._clock()

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
            # A rate per sentence; an empty field keeps the one already set.
            for name, value in zip(PCAS03_FIELDS, fields[1:]):
                if not value:
                    continue
                try:
                    self.rates[name] = int(value)
                except ValueError:
                    log.info("a %s rate of %r is not a number", name, value)
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
        now = self._now()
        if now is None:
            return b""
        # Fixes fall on the board's whole seconds, the first one a second after
        # power-on. Each is stamped with its own instant, not with whenever
        # this was asked, so the stamps are the same in every run. A fix missed
        # while the reader stalled is skipped, not queued.
        fix = int(now // FIX_INTERVAL)
        if fix <= self._fixes:
            return b""
        self._fixes = fix
        stamp = self.epoch + fix * FIX_INTERVAL
        if not self.sentences and any(self.rates[name] > 0 for name in SENT):
            log.info("first report, at %d s of the board's time", fix)
        out = b""
        for name in SENT:
            rate = self.rates[name]
            if rate > 0 and fix % rate == 0:
                out += sentence(getattr(self, f"_{name}")(stamp))
                self.sentences += 1
        return out

    def _position(self) -> str:
        lat, ns = degrees_to_nmea(self.latitude, "NS")
        lon, ew = degrees_to_nmea(self.longitude, "EW")
        return f"{lat},{ns},{lon},{ew}"

    def _gga(self, stamp: float) -> str:
        clock, _date = nmea_clock(stamp, places=3)
        return (f"GNGGA,{clock},{self._position()},1,{SATELLITES:02d},"
                f"{HDOP},{self.altitude_m:.1f},M,{GEOID_SEPARATION_M},M,,")

    def _gll(self, stamp: float) -> str:
        clock, _date = nmea_clock(stamp, places=3)
        return f"GNGLL,{self._position()},{clock},A,A"

    def _rmc(self, stamp: float) -> str:
        clock, date = nmea_clock(stamp, places=3)
        # Standing still: no speed and so no meaningful course. Magnetic
        # variation is not supported, and the navigational status is "not
        # valid", as in the specification's example.
        return f"GNRMC,{clock},A,{self._position()},0.00,0.00,{date},,,A,V"

    def _vtg(self, stamp: float) -> str:
        # Standing still. The magnetic course is not supported.
        return "GNVTG,0.00,T,,M,0.00,N,0.00,K,A"

    def _zda(self, stamp: float) -> str:
        clock, _date = nmea_clock(stamp, places=3)
        year, month, day = _civil_from_days(int(stamp) // 86400)
        # The local zone is not supported, and always reads 00,00.
        return f"GNZDA,{clock},{day:02d},{month:02d},{year},00,00"

    def summary(self) -> str:
        asked = ", ".join(sorted(set(self.commands))) or "nothing"
        return (f"L76K: {self.sentences} sentence(s) sent, asked for {asked}")


def nmea_clock(stamp: float, places: int = 2) -> tuple[str, str]:
    """A Unix time as NMEA's hhmmss and ddmmyy, in UTC, with `places`
    decimals of a second: u-blox writes two and the L76K three. A fraction of
    a second is not reported."""
    whole = int(stamp)
    seconds = whole % 60
    minutes = (whole // 60) % 60
    hours = (whole // 3600) % 24
    days = whole // 86400
    # Days since 1970 to a calendar date, without importing a timezone
    # database for a part that only ever reports UTC.
    year, month, day = _civil_from_days(days)
    return (f"{hours:02d}{minutes:02d}{seconds:02d}.{'0' * places}",
            f"{day:02d}{month:02d}{year % 100:02d}")


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


# -- the T-Deck Max's receiver ----------------------------------------------

UBX_SYNC = b"\xb5\x62"
# The NMEA sentences on by default on its UART (CFG-MSGOUT, Table 69 of the
# interface description) that a position and a time decide, in message-ID
# order. GSA and GSV, also on, describe a sky this model does not have.
UBLOX_SENT = ("gga", "gll", "rmc", "vtg")
# Each of them with no fix and no time: "it will output empty fields" (2.5.6).
UBLOX_NO_FIX = ("GNGGA,,,,,,0,00,99.99,,,,,,", "GNGLL,,,,,,V,N",
                "GNRMC,,V,,,,,,,,,,N,V", "GNVTG,,,,,,,,,N")
UBX_CFG = 0x06
UBX_ACK, UBX_ACK_ACK = 0x05, 0x01


def ubx_checksum(body: bytes) -> bytes:
    """u-blox's Fletcher-8, over class, id, length and payload."""
    a = b = 0
    for byte in body:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return bytes([a, b])


def ubx(cls: int, ident: int, payload: bytes = b"") -> bytes:
    body = bytes([cls, ident]) + len(payload).to_bytes(2, "little") + payload
    return UBX_SYNC + body + ubx_checksum(body)


class UBloxM10(SerialDevice):
    """The u-blox MIA-M10Q on the T-Deck Max's third UART (ADR 0105).

    Unlike the L76K it is never spoken to first: the factory demo listens, feeds
    every byte to TinyGPS++, and calls the module missing if fewer than ten
    characters have arrived after thirty seconds. So it streams from power-on,
    as the part does, once a second of the *board's* time (ADR 0104) -- on the
    host's clock a slow guest would be flooded, which is what kept the L76K
    silent until ADR 0116. Which sentences, and in what form, is u-blox's *M10
    SPG 5.10 Interface Description* (ADR 0117).

    It answers UBX configuration (class 0x06) with ACK-ACK, which the demo waits
    for after each command it sends. Without a position it is a receiver indoors:
    it streams, with no fix and no time, which is what the demo's "Place your
    board outside" expects. Given one, it has a fix there.

    A configuration message with no payload is a poll, answered with the
    configuration it names before the acknowledgement. Only CFG-RATE is
    answered -- a measurement every 1000 ms, one per solution, on GPS time,
    u-blox's default and the once-a-second this model streams at -- because it
    is the only poll a firmware here sends. Any other is logged, not invented.
    """

    CONFIGURATION = {
        0x08: (1000).to_bytes(2, "little") + (1).to_bytes(2, "little")
              + (1).to_bytes(2, "little"),        # CFG-RATE
    }

    def __init__(self, latitude: float | None = None, longitude: float | None = None,
                 altitude_m: float = 0.0, epoch: float | None = None,
                 clock=None):
        self.position = (None if latitude is None or longitude is None
                         else (latitude, longitude))
        self.altitude_m = altitude_m
        self.epoch = 1_767_225_600.0 if epoch is None else epoch  # 2026-01-01Z
        # The board's clock when there is one; the host's otherwise, which only
        # a test with no board needs.
        self._clock = clock
        self._started = time.monotonic()
        self._fixes = 0           # the last fix reported, in the board's seconds
        self._buffer = b""
        self.sentences = 0
        self.acknowledged: list[tuple[int, int]] = []
        self.ignored: list[str] = []

    def _now(self) -> float | None:
        if self._clock is None:
            return time.monotonic() - self._started
        return self._clock()

    # -- what the guest says to it ---------------------------------------

    def receive(self, data: bytes) -> bytes:
        self._buffer += data
        answer = b""
        while True:
            start = self._buffer.find(UBX_SYNC)
            if start < 0:
                self._note_text(self._buffer)
                self._buffer = b""
                break
            if start:
                self._note_text(self._buffer[:start])
                self._buffer = self._buffer[start:]
            if len(self._buffer) < 6:
                break
            length = int.from_bytes(self._buffer[4:6], "little")
            if len(self._buffer) < 8 + length:
                break
            frame, self._buffer = self._buffer[:8 + length], self._buffer[8 + length:]
            body, check = frame[2:6 + length], frame[6 + length:]
            if ubx_checksum(body) != check:
                log.warning("u-blox: a UBX frame with a bad checksum, dropped")
                continue
            cls, ident = body[0], body[1]
            if cls == UBX_CFG and length == 0:
                # A poll: answered with the configuration it names, then
                # acknowledged. The demo's check after CFG-RATE waits for the
                # 0x06/0x08 frame itself, not for the acknowledgement.
                current = self.CONFIGURATION.get(ident)
                if current is None:
                    self.ignored.append(f"poll CFG {ident:02x}")
                    log.info("u-blox: poll of CFG 0x%02x, no model", ident)
                    continue
                self.acknowledged.append((cls, ident))
                answer += ubx(cls, ident, current) + ubx(UBX_ACK, UBX_ACK_ACK,
                                                         bytes([cls, ident]))
            elif cls == UBX_CFG:
                self.acknowledged.append((cls, ident))
                log.info("u-blox: CFG 0x%02x acknowledged", ident)
                answer += ubx(UBX_ACK, UBX_ACK_ACK, bytes([cls, ident]))
            else:
                self.ignored.append(f"UBX {cls:02x}/{ident:02x}")
                log.info("u-blox: UBX 0x%02x/0x%02x, no model", cls, ident)
        return answer

    def _note_text(self, data: bytes) -> None:
        text = data.decode("ascii", "replace").strip()
        if text:
            self.ignored.append(text[:40])
            log.info("u-blox: %r, no model", text[:80])

    # -- what it says on its own -----------------------------------------

    def poll(self) -> bytes:
        now = self._now()
        if now is None:
            return b""
        # As the L76K's (ADR 0116): fixes on the board's whole seconds, the
        # first a second after power-on, each stamped with its own instant
        # rather than with whenever this was asked. One missed while the
        # reader stalled is skipped, not queued.
        fix = int(now // FIX_INTERVAL)
        if fix <= self._fixes:
            return b""
        self._fixes = fix
        self.sentences += len(UBLOX_SENT)
        if self.position is None:
            # No satellites, so no position and no time either.
            return b"".join(sentence(body) for body in UBLOX_NO_FIX)
        stamp = self.epoch + fix * FIX_INTERVAL
        return b"".join(sentence(getattr(self, f"_{name}")(stamp))
                        for name in UBLOX_SENT)

    def _position(self) -> str:
        lat, ns = degrees_to_nmea(self.position[0], "NS")
        lon, ew = degrees_to_nmea(self.position[1], "EW")
        return f"{lat},{ns},{lon},{ew}"

    def _gga(self, stamp: float) -> str:
        clock, _date = nmea_clock(stamp)
        return (f"GNGGA,{clock},{self._position()},1,{SATELLITES:02d},"
                f"{HDOP:.2f},{self.altitude_m:.1f},M,{GEOID_SEPARATION_M:.1f},M,,")

    def _gll(self, stamp: float) -> str:
        clock, _date = nmea_clock(stamp)
        return f"GNGLL,{self._position()},{clock},A,A"

    def _rmc(self, stamp: float) -> str:
        clock, date = nmea_clock(stamp)
        # Standing still: the speed is 0.000 knots and the course is left
        # empty, as in the receiver output quoted in 2.5.3.
        return f"GNRMC,{clock},A,{self._position()},0.000,,{date},,,A,V"

    def _vtg(self, stamp: float) -> str:
        # Standing still, as in RMC: no course, and zero speed in both units.
        return "GNVTG,,T,,M,0.000,N,0.000,K,A"

    def summary(self) -> str:
        cfg = ", ".join(f"0x{i:02x}" for _c, i in self.acknowledged) or "none"
        return (f"u-blox M10: {self.sentences} sentence(s) sent, "
                f"{'a fix' if self.position else 'no fix'}, CFG acknowledged: {cfg}"
                + (f", ignored {len(self.ignored)}" if self.ignored else ""))
