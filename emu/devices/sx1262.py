#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The LoRa radio, as far as a host can tell without one (ADR 0034).

Everything the host learns about this part it learns over SPI, and every
exchange is framed by the chip select: an opcode, its parameters, and for a
read as many bytes as the host chooses to clock. The reply to byte N is decided
while byte N is being clocked, which is why this answers per byte rather than
per command, and applies the command once the window closes.

The chip answers every byte with a status: which mode it is in and how the last
command went. A host checks it constantly, and a part that answers `0xFF` --
which is what an empty chip select reads as -- looks to RadioLib like a radio
that is not there. That is precisely what both firmwares concluded.

There is no radio here and nothing is transmitted. What this models is the
conversation: the firmware configures a radio, is told the configuration took,
and hears no traffic, which is what a real node alone in a room experiences.

It also says how long each command keeps it busy, which the SPI controller
holds its BUSY pin high for on the guest's clock (ADR 0111). The times are the
datasheet's -- Semtech DS.SX1261-2.W.APP, Rev. 1.2 -- and where the datasheet
gives none, there is none.

And it keeps the guest's time, not the host's (ADR 0112). Every segment says
when the guest sent it; what the radio does by itself -- a packet's last bit,
a sweep's end -- is a queue of events at guest times, which settle whenever
the guest's time is next known to have passed them.

On an air whose boards keep one time (ADR 0113), what others sent is judged
at each barrier on that shared clock: against a history of this radio's modes
with the exact times its segments gave, so a receiver that entered RX late,
left it early or was transmitting is judged as it was, and a CAD sees what was
on the air while it listened.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import math
import threading
from typing import NamedTuple

from emu.devices import lora
from emu.spi_link import BUSY_HELD, SpiDevice

log = logging.getLogger("spi")

REGISTER_SPACE = 0x1000
BUFFER_SIZE = 256

# Opcodes, named as the datasheet names them. Only what a firmware has sent.
SET_SLEEP = 0x84
SET_STANDBY = 0x80
SET_FS = 0xC1
SET_TX = 0x83
SET_RX = 0x82
STOP_TIMER_ON_PREAMBLE = 0x9F
SET_RX_DUTY_CYCLE = 0x94
SET_CAD = 0xC5              # listen before talking: is the channel busy?
SET_CAD_PARAMS = 0x88
CALIBRATE = 0x89
CALIBRATE_IMAGE = 0x98
SET_PA_CONFIG = 0x95
SET_RX_TX_FALLBACK = 0x93
WRITE_REGISTER = 0x0D
READ_REGISTER = 0x1D
WRITE_BUFFER = 0x0E
READ_BUFFER = 0x1E
SET_DIO_IRQ_PARAMS = 0x08
GET_IRQ_STATUS = 0x12
CLEAR_IRQ_STATUS = 0x02
SET_DIO2_AS_RF_SWITCH = 0x9D
SET_DIO3_AS_TCXO = 0x97
SET_RF_FREQUENCY = 0x86
SET_PACKET_TYPE = 0x8A
GET_PACKET_TYPE = 0x11
SET_TX_PARAMS = 0x8E
SET_MODULATION_PARAMS = 0x8B
SET_PACKET_PARAMS = 0x8C
SET_BUFFER_BASE = 0x8F
SET_LORA_SYMB_TIMEOUT = 0xA0
GET_STATUS = 0xC0
GET_RSSI_INST = 0x15
GET_RX_BUFFER_STATUS = 0x13
GET_PACKET_STATUS = 0x14
GET_DEVICE_ERRORS = 0x17
CLEAR_DEVICE_ERRORS = 0x07
SET_REGULATOR_MODE = 0x96

# Interrupt flags, in the register a host reads after being nudged.
IRQ_TX_DONE = 0x0001
IRQ_RX_DONE = 0x0002
IRQ_PREAMBLE_DETECTED = 0x0004
IRQ_HEADER_VALID = 0x0010
IRQ_CAD_DONE = 0x0080
IRQ_CAD_DETECTED = 0x0100       # bit 8 (Table 8-4); 0x0040 is CrcErr
# What a reception raises, in the order it raises them (ADR 0141).
RECEPTION_FLAGS = (IRQ_PREAMBLE_DETECTED, IRQ_HEADER_VALID, IRQ_RX_DONE)

# A LoRa packet after its programmed preamble: 4.25 symbols of sync word and
# start of frame, then the eight symbols the time-on-air equation always
# counts first, coded at 4/8, which carry an explicit header (6.1.3.1, 6.1.4).
SYNC_SYMBOLS = 4.25
HEADER_SYMBOLS = 8
# SetPacketParams' header type: a fixed-length packet has no header (13.4.6).
HEADER_IMPLICIT = 0x01

# Chip modes, in the top nibble of the status byte.
MODE_STANDBY_RC = 2
MODE_STANDBY_XOSC = 3
MODE_FS = 4
MODE_RX = 5
MODE_TX = 6

# Command status, in bits 3:1. Three, four and five are the failures a host
# reacts to, so a working part reports none of them.
COMMAND_OK = 2

# How long BUSY stays high after a window that asks for a mode, by the mode the
# chip was in and the mode asked for: Table 8-2, typical, in microseconds. A
# switch the table does not list has no published time and is given none.
SWITCH_US = {
    (MODE_STANDBY_RC, MODE_STANDBY_XOSC): 31,
    (MODE_STANDBY_RC, MODE_FS): 50,
    (MODE_STANDBY_RC, MODE_RX): 83,
    (MODE_STANDBY_RC, MODE_TX): 126,
    (MODE_STANDBY_XOSC, MODE_FS): 40,
    (MODE_STANDBY_XOSC, MODE_TX): 105,
    (MODE_STANDBY_XOSC, MODE_RX): 62,
    (MODE_FS, MODE_RX): 41,
    (MODE_FS, MODE_TX): 76,
    (MODE_RX, MODE_FS): 15,
    (MODE_RX, MODE_TX): 92,
}
# Leaving sleep, from NSS falling to STBY_RC: Table 8-2. A cold start is also
# what a reset costs, since start-up runs the same power-on calibration (8.2.2,
# 9.1, 9.2).
WAKE_WARM_US = 340
WAKE_COLD_US = 3500
SLEEP_WARM = 0x04               # sleepConfig bit 2 (Table 13-2)
# Calibrating every block (13.1.12). No time is given for fewer.
CALIBRATE_US = 3500
# Symbols a CAD listens for, by SetCadParams' code (Table 13-72). It then
# stays in RX about half a symbol more to post-process (6.1.5).
CAD_SYMBOLS = {0x00: 1, 0x01: 2, 0x02: 4, 0x03: 8, 0x04: 16}
CAD_POST_SYMBOLS = 0.5
# Where the chip goes after a transmission or a reception, by
# SetRxTxFallbackMode's byte (13.1.15, Table 13-23). STBY_RC unless told.
FALLBACK = {0x40: MODE_FS, 0x30: MODE_STANDBY_XOSC, 0x20: MODE_STANDBY_RC}
# SetRx's timeout that means "stay in RX" (13.1.5); anything else receives one
# packet and falls back.
RX_CONTINUOUS = 0xFFFFFF
# SetCadParams' exit mode (13.4.7): back to STBY_RC, or stay in RX on detection.
CAD_ONLY, CAD_RX = 0x00, 0x01
# A CAD in the mode history: listening, but not receiving (ADR 0113).
MODE_CAD = -1
# How long packets heard on a shared air are kept for a CAD to look back on.
KEEP_HEARD_NS = 1_000_000_000


def lock_symbols(spreading: int) -> int:
    """Preamble symbols a receiver needs still to come to lock on, when it
    enters RX after a packet has started. The datasheet gives none; this is
    the figure the firmware's own library sizes a duty cycle by (RadioLib,
    calculateRxDutyCycle): 8, or 12 at SF5 and SF6 (ADR 0113)."""
    return 12 if spreading <= 6 else 8


class Heard(NamedTuple):
    """A packet on the air at this radio: when, from whom, how strong."""

    start_ns: int
    end_ns: int
    sender: str
    payload: bytes
    arrived_dbm: float
# SetDIO3AsTCXOCtrl's delay is in these units, and a switch out of STBY_RC
# into a mode that needs the clock waits that long for it (13.3.6).
TCXO_STEP_US = 15.625

# Where the part keeps the string a host reads to find out what it is talking
# to. RadioLib compares the first characters against the model it expects, and
# a mismatch is reported as no chip at all.
VERSION_ADDRESS = 0x0320
# What the part answers when a host asks what it is. Not a guess and not the
# name on the module: both firmwares refuse "SX1262" and accept this, which is
# the only evidence available and is what ADR 0006 says to follow. With it the
# vendor build prints "Start Radio success!" and Meshtastic goes on to
# configure the radio; with "SX1262" both give up.
VERSION_STRING = b"SX1261"

# Where the contents start in a register read: the opcode, two address bytes
# and one of padding come first, which is what RadioLib's own framing gives.
DATA_INDEX = 4


class Sx1262(SpiDevice):
    """Answers the SX126x command set over one chip select."""

    name = "sx1262"

    def __init__(self, air=None, signal=None,
                 position: tuple[float, float] = (0.0, 0.0),
                 antenna_dbi: float = 0.0,
                 exponent: float = lora.FREE_SPACE_EXPONENT,
                 obstruction_db: float = 0.0, clock=None) -> None:
        # `air` carries frames to and from other boards. The interrupt line
        # is the SPI controller's to drive, from line() (ADR 0112); `signal`
        # drives it from this side for the one event that arrives off the air
        # rather than through a command, a packet received.
        self.air = air
        self.signal = signal
        # The guest's time in seconds from the board's clock tick (ADR 0104),
        # for the ticker, which has no segment to read it from. None, or
        # answering None, until the first tick.
        self.clock = clock
        self._lock = threading.RLock()
        self._order = itertools.count()
        # On a shared air (ADR 0113), the barrier judges what was heard, and
        # pump() leaves the air alone.
        self.timed = bool(getattr(air, "timed", False))
        # This radio's modes, (since_ns, mode), and the packets on the air it
        # has not yet decided about, or that a CAD may still look back on.
        # Physics and record, not chip state: a reset keeps them.
        self._history: list[tuple[int, int]] = []
        self._candidates: list[Heard] = []
        self._recent: list[Heard] = []
        # Frames in hand but not yet known: a frame counts from the first
        # barrier after it started (ADR 0115).
        self._arriving: list[tuple[str, bytes]] = []
        self.cut_off = 0
        self.cad_detected = 0
        self.sent = 0
        self.heard = 0
        self.cad_sweeps = 0
        self.collisions = 0
        self.wrong_frequency = 0
        self.out_of_range = 0
        # Packets that reached this radio while it could not have received
        # them: transmitting (a radio is deaf while it does, ADR 0037), or not
        # in receive mode. They used to vanish uncounted, so "sent one, heard
        # none" could not say whether two nodes had talked over each other.
        self.deaf = 0
        self.not_listening = 0
        # Where this board stands, in metres. Supplied by whoever is setting up
        # the scenario; there is no distance between two processes to discover
        # (ADR 0038).
        self.position = position
        # What the aerial on this board adds, and what the place between the
        # boards costs beyond bare geometry. Both are the scenario's to state
        # (ADR 0039); the defaults are a plain aerial in free space, which is
        # what every run had before.
        self.antenna_dbi = antenna_dbi
        self.exponent = exponent
        self.obstruction_db = obstruction_db
        # What the last packet arrived at, which the host reads back.
        self.last_rssi_dbm = -157.0
        self.last_snr_db = -20.0
        self.frame = bytearray()
        self.commands = 0
        self.unknown: set[int] = set()
        # What the BUSY line owes for the window that just closed, collected
        # by busy() (ADR 0111).
        self._busy = (0, 0)
        self._power_on()

    def _power_on(self) -> None:
        """The chip as a reset or a cold start leaves it: STBY_RC, nothing
        flagged or routed, no TCXO, and registers, buffer and modulation as
        the model first had them. The run's own records -- what was sent and
        heard -- are not the chip's and are kept."""
        self.registers = bytearray(REGISTER_SPACE)
        self.registers[VERSION_ADDRESS:VERSION_ADDRESS + len(VERSION_STRING)] = VERSION_STRING
        self.buffer = bytearray(BUFFER_SIZE)
        self._receiving: Heard | None = None
        self._set_mode(MODE_STANDBY_RC, self.now_ns)
        self.irq = 0
        # SetDioIrqParams: which flags are logged at all, and which of those
        # raise DIO1. Both nothing at power-on (13.3.1, 13.3.2).
        self.irq_mask = 0
        self.dio1_mask = 0
        self.header_implicit = False
        self.packet_type = 0
        self.frequency = 0
        self.tx_base = 0
        self.rx_base = 0
        self.payload_length = 0
        self.received_length = 0
        # What the host asked the part to transmit at, in dBm.
        self.transmit_dbm = 22.0
        # The modulation the host programs, which is what decides how long a
        # packet of a given length occupies the channel (ADR 0037).
        self.spreading = 7
        self.bandwidth_code = 0x04
        self.coding_rate = 1
        self.low_data_rate = False
        self.preamble = 8
        # While transmitting, this part is deaf; while a packet is arriving,
        # a second one lands on top of it and neither survives. Guest
        # nanoseconds. A reset ends both, and with them everything pending.
        self.tx_until_ns = 0
        self.rx_until_ns = 0
        # What the radio will do by itself, in guest-time order:
        # (at_ns, order, flag, packet).
        self._due: list[tuple[int, int, int, tuple | None]] = []
        self.fallback = MODE_STANDBY_RC
        self.rx_single = False
        self.cad_symbols: int | None = None
        self.cad_exit = CAD_ONLY
        # A reception scheduled since the controller last asked about the
        # line, so one it cannot know to raise (ADR 0112).
        self._rx_untold = False
        # The TCXO's start-up, once the host has said there is one: added to
        # a switch out of STBY_RC (ADR 0111).
        self.tcxo_us = 0.0
        self.asleep = False
        self.sleep_warm = False
        self.in_reset = False

    # -- the bus ------------------------------------------------------------

    @property
    def status(self) -> int:
        return ((self.mode & 0x7) << 4) | ((COMMAND_OK & 0x7) << 1)

    def on_segment(self, dc: int, data: bytes, duplex: bool) -> bytes | None:
        with self._lock:
            self._settle(self.now_ns)
            if self.asleep:
                # The select falling is what wakes it (13.1.1). A cold start
                # is a power-on: what the chip was told before sleeping is gone.
                self.asleep = False
                if not self.sleep_warm:
                    log.info("sx1262: woke from cold sleep at guest %.6f s, "
                             "at its power-on settings", self.now_ns / 1e9)
                    self._power_on()
            reply = bytearray()
            for byte in data:
                reply.append(self._reply_at(len(self.frame)))
                self.frame.append(byte)
            return bytes(reply) if duplex else None

    def on_deselect(self) -> None:
        with self._lock:
            self._settle(self.now_ns)
            self._busy = (0, 0)
            if self.frame and not self.in_reset:
                self._apply(bytes(self.frame))
                self._settle(self.now_ns)
            self.frame = bytearray()

    def on_reset(self, asserted: bool) -> None:
        """NRESET moved. Held low, the chip is in reset and BUSY is high
        (Table 8-3); released, it starts up and BUSY falls once it is in
        STBY_RC, a cold start's time later (ADR 0111)."""
        with self._lock:
            self._power_on()
            self.in_reset = asserted
            # At INFO: a radio found at its power-on settings after a reboot
            # is the one open case of ADR 0141, and this says when it reset.
            log.info("sx1262: NRESET %s at guest %.6f s",
                     "held" if asserted else "released", self.now_ns / 1e9)
            if asserted:
                self._busy = (BUSY_HELD, BUSY_HELD)
            else:
                self._busy = (WAKE_COLD_US * 1000, 0)

    def busy(self) -> tuple[int, int]:
        with self._lock:
            owed, self._busy = self._busy, (0, 0)
            return owed

    def line(self) -> tuple[int | None, int]:
        """DIO1: high while a logged flag is routed to it, and otherwise
        rising when the first routed event falls due (ADR 0112). A flag is
        logged if IrqMask enables it and raises DIO1 if Dio1Mask routes it
        too (13.3.2.1, ADR 0141)."""
        with self._lock:
            self._rx_untold = False
            if self.irq & self.dio1_mask:
                return (1, 0)
            routed = [at for at, _, flag, _ in self._due
                      if flag & self.irq_mask & self.dio1_mask]
            return (0, min(routed) if routed else 0)

    def _switch(self, to: int) -> None:
        """Owe BUSY the time Table 8-2 gives for going from the mode the chip
        is in to `to`, and the TCXO's start-up on top when the clock was off.
        Then be in that mode."""
        us = SWITCH_US.get((self.mode, to), 0)
        if us and self.mode == MODE_STANDBY_RC and self.tcxo_us:
            us += self.tcxo_us
        self._busy = (int(us * 1000), 0)
        # The status says the new mode at once; the chip is in it once BUSY
        # falls, and that is when it can hear anything (8.3.1).
        self._set_mode(to, self.now_ns + self._busy[0])

    def _set_mode(self, mode: int, at_ns: int, history: int | None = None) -> None:
        """Be in `mode` from `at_ns`, and keep the record ADR 0113 judges by.
        Leaving RX part-way through a packet loses it."""
        self.mode = mode
        bisect.insort(self._history, (at_ns, mode if history is None else history))
        held = mode if history is None else history
        if (self._receiving is not None and held != MODE_RX
                and at_ns < self._receiving.end_ns):
            self._drop_reception()
            self._receiving = None
            self.cut_off += 1

    def _drop_reception(self) -> None:
        """A reception lost before it ended: whatever of its flags has not
        been raised yet never will be. What was raised stays logged until the
        host clears it, as on the chip."""
        self._due = [due for due in self._due if due[2] not in RECEPTION_FLAGS]

    def guest_ns(self) -> int:
        """The latest guest time known: the last segment's, or the clock's
        tick if that is later."""
        ns = self.now_ns
        seconds = self.clock() if self.clock is not None else None
        if seconds is not None:
            ns = max(ns, int(seconds * 1e9))
        return ns

    def pump(self) -> None:
        """Take anything off the air, and let what has fallen due happen.

        Called from the board's ticker rather than on bus traffic, because an
        idle guest makes almost none. A packet received is the one event that
        can come from outside a command: if the controller has not been asked
        about the line since it was scheduled, it cannot raise it, so it goes
        through `signal`. Anything else the controller already raised on the
        guest's clock, and a second, later write from here could only land
        after the host had cleared it.
        """
        if self.timed:
            return
        with self._lock:
            now_ns = self.guest_ns()
            raised = self._settle(now_ns)
            if self.air is not None:
                self._listen(now_ns)
            raised |= self._settle(now_ns)
            if (raised & IRQ_RX_DONE & self.dio1_mask and self._rx_untold
                    and self.signal is not None):
                self._rx_untold = False
                self.signal(True)

    def _schedule(self, at_ns: int, flag: int, packet: tuple | None = None) -> None:
        bisect.insort(self._due, (at_ns, next(self._order), flag, packet))

    def _settle(self, now_ns: int) -> int:
        """Everything due by `now_ns` happens, in order. Returns the flags
        raised."""
        raised = 0
        while self._due and self._due[0][0] <= now_ns:
            _at, _order, flag, packet = self._due.pop(0)
            if flag == IRQ_TX_DONE:
                # The last bit is out: the PA ramps down and the chip goes to
                # its fallback mode, if the host has not moved it (13.1.4).
                self.tx_until_ns = 0
                if self.mode == MODE_TX:
                    self._set_mode(self.fallback, _at)
            elif flag == IRQ_RX_DONE and packet is not None:
                self._receiving = None
                self._received(*packet, _at)
                if self.rx_single and self.mode == MODE_RX:
                    self._set_mode(self.fallback, _at)
            elif flag == IRQ_CAD_DONE:
                flag |= self._cad_done(_at, packet)
            # Logged only if enabled (13.3.2); a masked source never shows.
            logged = flag & self.irq_mask
            self.irq |= logged
            raised |= logged
        return raised

    def _received(self, payload: bytes, arrived: float, at_ns: int) -> None:
        payload = payload[:BUFFER_SIZE]
        for offset, value in enumerate(payload):
            self.buffer[(self.rx_base + offset) % BUFFER_SIZE] = value
        self.received_length = len(payload)
        self.last_rssi_dbm = arrived
        self.last_snr_db = arrived - lora.noise_floor_dbm(self.bandwidth_code)
        self.heard += 1
        # One line per packet received: they are rare, and a reception is
        # the event a mesh run waits for (ADR 0098). The guest's time makes
        # two runs comparable (ADR 0113).
        log.info("sx1262: heard a %d-byte packet at %.1f dBm at guest %.6f s",
                 len(payload), arrived, at_ns / 1e9)

    def _cad_done(self, at_ns: int, window: tuple | None) -> int:
        """A sweep ends. It detects any packet that overlapped its symbols by
        a symbol or more -- "if LoRa symbols have been detected during the
        CAD" (6.1.5) -- and the chip returns to STBY_RC, or stays in RX on
        detection if told CAD_RX (13.1.8, 13.4.7). On either air: on the
        wall-clock one a packet starts when this board reads it, and is
        judged from there (ADR 0141, closing ADR 0111's exception)."""
        found = False
        if window is not None:
            start, end = window
            symbol = int(lora.symbol_time(self.spreading, self.bandwidth_code) * 1e9)
            found = any(min(p.end_ns, end) - max(p.start_ns, start) >= symbol
                        for p in self._recent)
        if found:
            self.cad_detected += 1
        self._set_mode(MODE_RX if found and self.cad_exit == CAD_RX
                       else MODE_STANDBY_RC, at_ns)
        return IRQ_CAD_DETECTED if found else 0

    def _intake(self, frame: bytes, sender: str, start_ns: int | None) -> Heard | None:
        """A frame off the air, as this radio would see it: None if it is on
        another channel or too weak to decode, which are counted. It starts at
        `start_ns`, or on a shared air at its sender's start (ADR 0113)."""
        decoded = lora.decode(frame)
        if decoded is None:
            return None
        if decoded.frequency != self.frequency:
            # Tuned elsewhere. Nothing about being on the same air makes
            # two radios on different channels hear each other.
            self.wrong_frequency += 1
            return None
        # Whether it arrives strongly enough to be decoded at all. The
        # distance is the scenario's, the rest is arithmetic (ADR 0038).
        distance = math.dist(self.position, decoded.origin)
        arrived = lora.received_dbm(
            decoded.power_dbm, distance, lora.register_to_hz(decoded.frequency),
            exponent=self.exponent,
            transmit_gain_dbi=decoded.gain_dbi,
            receive_gain_dbi=self.antenna_dbi,
            shadowing_db=self.obstruction_db)
        floor = lora.sensitivity_dbm(self.bandwidth_code, self.spreading)
        if arrived < floor:
            self.out_of_range += 1
            log.debug("sx1262: %.1f dBm at %.0f m is under %.1f dBm",
                      arrived, distance, floor)
            return None
        start = round(decoded.sent_at * 1e9) if start_ns is None else start_ns
        return Heard(start, start + int(decoded.airtime * 1e9), sender,
                     decoded.payload, arrived)

    # -- a shared air (ADR 0113) --------------------------------------------

    def barrier(self, ns: int, frames: list[tuple[str, bytes]]) -> tuple[int | None, int]:
        """Every board on the air has reached guest time `ns`. Take what they
        sent, judge what can now be judged, and say where DIO1 goes."""
        with self._lock:
            self.now_ns = max(self.now_ns, ns)
            self._arriving += frames
            # What this board knows of the air at a barrier is fixed by the
            # barrier, not by when frames happened to arrive. Every frame that
            # started before `ns` was sent before its sender reached this
            # barrier, so it is surely in hand; one that started later may or
            # may not be, depending on how far the host let its sender run,
            # and waits for the barrier after its start (ADR 0115).
            known, self._arriving = self._split_known(ns)
            for sender, frame in known:
                heard = self._intake(frame, sender, None)
                if heard is not None:
                    self._candidates.append(heard)
                    self._recent.append(heard)
            self._candidates.sort()
            self._settle(ns)
            self._judge(ns)
            self._settle(ns)
            self._trim(ns)
            return self.line()

    def _split_known(self, ns: int) -> tuple[list, list]:
        """The frames in hand that started before `ns`, in an order of their
        own -- start, sender, contents -- and those that did not."""
        known, later = [], []
        for sender, frame in self._arriving:
            decoded = lora.decode(frame)
            start = round(decoded.sent_at * 1e9) if decoded is not None else ns - 1
            (known if start < ns else later).append((start, sender, frame))
        known.sort()
        return ([(sender, frame) for _, sender, frame in known],
                [(sender, frame) for _, sender, frame in later])

    def _judge(self, now_ns: int) -> None:
        """Decide every packet that started by `now_ns` and can be decided:
        received if this radio was in RX at its start or entered RX while
        enough preamble was still to come, and stayed long enough to lock on;
        otherwise deaf if it was transmitting meanwhile, or not listening."""
        undecided = []
        for heard in self._candidates:
            if heard.start_ns > now_ns:
                undecided.append(heard)
                continue
            if ((self._receiving is not None and heard.start_ns < self._receiving.end_ns)
                    or heard.start_ns < self.rx_until_ns):
                # Two on top of each other is what a collision is, and
                # neither survives it.
                self.collisions += 1
                self._drop_reception()
                self._receiving = None
                self.rx_until_ns = max(self.rx_until_ns, heard.end_ns)
                self.received_length = 0
                continue
            symbol = lora.symbol_time(self.spreading, self.bandwidth_code)
            spare = max(0.0, self.preamble + 4.25 - lock_symbols(self.spreading))
            deadline = heard.start_ns + int(spare * symbol * 1e9)
            locked = self._locks(heard, min(now_ns, deadline), symbol * 1e9)
            if locked is not None:
                # Known only now: a radio that has left RX since it locked
                # lost the packet then, and is not credited with it (ADR 0141).
                if self._left(MODE_RX, locked, min(now_ns, heard.end_ns)):
                    self.cut_off += 1
                    self.rx_until_ns = max(self.rx_until_ns, heard.end_ns)
                    continue
                self._take(heard, locked)
            elif now_ns >= deadline:
                if self._first(MODE_TX, heard.start_ns, deadline) is not None:
                    self.deaf += 1      # transmitting, and deaf while it does
                else:
                    self.not_listening += 1
            else:
                undecided.append(heard)
        self._candidates = undecided

    def _take(self, heard: Heard, entered_ns: int) -> None:
        """Receive `heard`, from `entered_ns`, when the radio was first in RX
        for it. The preamble is detected once the receiver has locked, the
        header is valid once it has been decoded, and the packet is in at its
        last bit (Table 8-4, ADR 0141). The datasheet gives no time for the
        first; the lock figure ADR 0113 already uses stands in for it."""
        symbol = lora.symbol_time(self.spreading, self.bandwidth_code) * 1e9
        header_ns = heard.start_ns + int(
            (self.preamble + SYNC_SYMBOLS + HEADER_SYMBOLS) * symbol)
        preamble_ns = min(max(heard.start_ns, entered_ns)
                          + int(lock_symbols(self.spreading) * symbol), header_ns)
        self._receiving = heard
        self.rx_until_ns = heard.end_ns
        self._schedule(min(preamble_ns, heard.end_ns), IRQ_PREAMBLE_DETECTED)
        if not self.header_implicit:
            self._schedule(min(header_ns, heard.end_ns), IRQ_HEADER_VALID)
        self._schedule(heard.end_ns, IRQ_RX_DONE, (heard.payload, heard.arrived_dbm))
        self._rx_untold = True

    def _locks(self, heard: Heard, latest: int, symbol_ns: float) -> int | None:
        """When this radio entered RX for `heard` and stayed there long enough
        to lock on: in RX by `latest`, and still in RX the lock's symbols
        later (ADR 0113). A radio in RX for an instant and then transmitting
        never locked, so it was deaf, not cut off (ADR 0141). An RX not yet
        left is taken to stay; leaving it later cuts the reception off."""
        need = lock_symbols(self.spreading) * symbol_ns
        history = self._history
        for index, (since, held) in enumerate(history):
            if held != MODE_RX or (index and history[index - 1][1] == MODE_RX):
                continue                        # not where an RX stretch begins
            until = next((s for s, h in history[index + 1:] if h != MODE_RX), None)
            entry = max(since, heard.start_ns)
            if entry > latest or (until is not None and until <= heard.start_ns):
                continue
            if until is None or until >= entry + need:
                return entry
        return None

    def _left(self, mode: int, start: int, end: int) -> bool:
        """Whether this radio was in some other mode at any time in
        (start, end]."""
        return any(start < since <= end and held != mode
                   for since, held in self._history)

    def _trim(self, ns: int) -> None:
        """Forget what no packet still undecided, arriving or swept for can
        need: packets heard too long ago, and modes older than the oldest
        packet still in question."""
        self._recent = [p for p in self._recent if p.end_ns >= ns - KEEP_HEARD_NS]
        if self._candidates or self._receiving:
            since = min([p.start_ns for p in self._candidates]
                        + ([self._receiving.start_ns] if self._receiving else []))
        else:
            since = ns
        keep = max(0, bisect.bisect_right(self._history, (since, 1 << 30)) - 1)
        del self._history[:keep]

    def _first(self, mode: int, start: int, end: int) -> int | None:
        """The first time in [start, end] this radio was in `mode`."""
        for index, (since, held) in enumerate(self._history):
            until = (self._history[index + 1][0] if index + 1 < len(self._history)
                     else end + 1)
            if held != mode or until <= start or since > end:
                continue
            return max(since, start)
        return None

    def _listen(self, now_ns: int) -> None:
        """Take what has arrived off the air, on this board's own guest clock.

        Boards on this air do not share a clock (ADR 0113), so a packet starts
        when this board reads it and occupies it for its time on air from then
        (ADR 0112). From there it is judged as on a shared air: against this
        radio's modes, so a sweep sees it, a radio that enters RX while enough
        preamble is left still locks on, and one that was transmitting or not
        listening is counted so once its preamble has gone (ADR 0141).
        """
        for frame in self.air.poll():
            heard = self._intake(frame, "", now_ns)
            if heard is not None:
                self._candidates.append(heard)
                self._recent.append(heard)
        self._candidates.sort()
        self._judge(now_ns)
        self._trim(now_ns)

    # -- what the host reads while it clocks --------------------------------

    def _reply_at(self, index: int) -> int:
        """The byte going out while byte `index` of this window comes in.

        The part answers with its status throughout, except where the host is
        clocking out something it asked for, and then it answers with that.
        """
        if index == 0:
            return self.status
        opcode = self.frame[0]

        if opcode == READ_REGISTER and index >= DATA_INDEX:
            # opcode, two address bytes, one of padding, then the contents
            address = (self.frame[1] << 8) | self.frame[2]
            return self._register(address + index - DATA_INDEX)
        if opcode == READ_BUFFER and index >= 3:
            return self.buffer[(self.frame[1] + index - 3) % BUFFER_SIZE]
        if opcode == GET_IRQ_STATUS:
            return (self.status, self.status, (self.irq >> 8) & 0xFF,
                    self.irq & 0xFF)[min(index, 3)]
        if opcode == GET_RX_BUFFER_STATUS:
            # status, then how many bytes arrived and where they start.
            return (self.status, self.status, self.received_length,
                    self.rx_base)[min(index, 3)]
        if opcode == GET_PACKET_STATUS:
            # Strength in negative half-decibels and signal-to-noise in
            # quarter-decibels, both as the part reports them, and both worked
            # out from the link the packet actually crossed (ADR 0038).
            rssi = max(0, min(255, int(round(-2 * self.last_rssi_dbm))))
            snr = int(round(self.last_snr_db * 4))
            snr = max(-128, min(127, snr)) & 0xFF
            return (self.status, self.status, rssi, snr, rssi)[min(index, 4)]
        if opcode == GET_DEVICE_ERRORS:
            return (self.status, self.status, 0x00, 0x00)[min(index, 3)]
        if opcode == GET_PACKET_TYPE:
            return (self.status, self.status, self.packet_type)[min(index, 2)]
        return self.status

    def airtime(self, payload: int) -> float:
        """How long a packet of this length holds the channel, from the
        modulation the host programmed."""
        return lora.time_on_air(payload, self.spreading, self.bandwidth_code,
                                self.coding_rate, self.low_data_rate,
                                self.preamble)

    def _transmit(self) -> None:
        """Put the packet the host wrote into the buffer on the air.

        It starts when the PA has ramped, which is when BUSY falls (8.3.1),
        and holds the channel for as long as the packet takes; TxDone waits
        for its last bit (13.1.4). A host told instantly would key the radio
        again before the first packet had left. All on the guest's clock
        (ADR 0112).
        """
        start_ns = self.now_ns + self._busy[0]
        if not self.payload_length:
            self._schedule(start_ns, IRQ_TX_DONE)
            return
        payload = bytes(self.buffer[(self.tx_base + offset) % BUFFER_SIZE]
                        for offset in range(self.payload_length))
        duration = self.airtime(len(payload))
        self.tx_until_ns = start_ns + int(duration * 1e9)
        self._schedule(self.tx_until_ns, IRQ_TX_DONE)
        if self.air is not None:
            self.air.send(lora.encode(self.frequency, start_ns / 1e9, duration,
                                      payload, self.transmit_dbm, *self.position,
                                      gain_dbi=self.antenna_dbi))
        self.sent += 1
        log.info("sx1262: sent a %d-byte packet at guest %.6f s for %.3f ms",
                 len(payload), start_ns / 1e9, duration * 1000)

    def _register(self, address: int) -> int:
        return self.registers[address] if address < REGISTER_SPACE else 0

    # -- what the command does ----------------------------------------------

    def _apply(self, frame: bytes) -> None:
        opcode = frame[0]
        self.commands += 1
        log.debug("sx1262: %s", frame[:24].hex())

        if opcode == WRITE_REGISTER and len(frame) >= 3:
            address = (frame[1] << 8) | frame[2]
            for offset, value in enumerate(frame[3:]):
                if address + offset < REGISTER_SPACE:
                    self.registers[address + offset] = value
        elif opcode == WRITE_BUFFER and len(frame) >= 2:
            for offset, value in enumerate(frame[2:]):
                self.buffer[(frame[1] + offset) % BUFFER_SIZE] = value
        elif opcode == SET_STANDBY:
            self._switch(MODE_STANDBY_XOSC if len(frame) > 1 and frame[1]
                         else MODE_STANDBY_RC)
        elif opcode == SET_SLEEP:
            # BUSY is high for the whole of sleep, and falls a wake-up after
            # the select next falls (13.1.1, Table 8-2).
            self.sleep_warm = len(frame) > 1 and bool(frame[1] & SLEEP_WARM)
            self.asleep = True
            self._set_mode(MODE_STANDBY_RC, self.now_ns)
            self._busy = (BUSY_HELD, (WAKE_WARM_US if self.sleep_warm
                                      else WAKE_COLD_US) * 1000)
        elif opcode == SET_FS:
            self._switch(MODE_FS)
        elif opcode == SET_TX:
            self._switch(MODE_TX)
            self._transmit()
        elif opcode == SET_RX:
            self._switch(MODE_RX)
            self.rx_single = (len(frame) < 4
                              or int.from_bytes(frame[1:4], "big") != RX_CONTINUOUS)
        elif opcode == SET_CAD:
            # Listening before talking. Answering that the sweep finished and
            # found nothing is what lets a host go on to transmit; saying
            # nothing at all leaves it waiting here for ever, which is exactly
            # where both firmwares stopped. The sweep lasts its symbols and
            # half a symbol more (6.1.5); a host that never set them is
            # answered at once, as before.
            self._set_mode(MODE_RX, self.now_ns, history=MODE_CAD)
            self.cad_sweeps += 1
            symbol = lora.symbol_time(self.spreading, self.bandwidth_code) * 1e9
            listens = 0 if self.cad_symbols is None else self.cad_symbols
            post = 0.0 if self.cad_symbols is None else CAD_POST_SYMBOLS
            self._schedule(self.now_ns + int((listens + post) * symbol), IRQ_CAD_DONE,
                           (self.now_ns, self.now_ns + int(listens * symbol)))
        elif opcode == SET_CAD_PARAMS and len(frame) >= 2:
            self.cad_symbols = CAD_SYMBOLS.get(frame[1])
            self.cad_exit = frame[4] if len(frame) >= 5 else CAD_ONLY
        elif opcode == SET_RX_TX_FALLBACK and len(frame) >= 2:
            self.fallback = FALLBACK.get(frame[1], MODE_STANDBY_RC)
        elif opcode == CALIBRATE:
            self._busy = (CALIBRATE_US * 1000, 0)
        elif opcode == SET_DIO3_AS_TCXO and len(frame) >= 5:
            self.tcxo_us = int.from_bytes(frame[2:5], "big") * TCXO_STEP_US
        elif opcode == CLEAR_IRQ_STATUS and len(frame) >= 3:
            # The line follows the flags, so clearing them lowers it -- in the
            # answer to this release (line()) -- and the next event is an edge.
            self.irq &= ~((frame[1] << 8) | frame[2]) & 0xFFFF
        elif opcode == SET_DIO_IRQ_PARAMS and len(frame) >= 3:
            # IrqMask, then Dio1Mask: logged, and routed to DIO1 (13.3.1).
            self.irq_mask = (frame[1] << 8) | frame[2]
            self.dio1_mask = (frame[3] << 8) | frame[4] if len(frame) >= 5 else 0
        elif opcode == SET_PACKET_TYPE and len(frame) >= 2:
            self.packet_type = frame[1]
        elif opcode == SET_RF_FREQUENCY and len(frame) >= 5:
            self.frequency = int.from_bytes(frame[1:5], "big")
        elif opcode == SET_BUFFER_BASE and len(frame) >= 3:
            self.tx_base, self.rx_base = frame[1], frame[2]
        elif opcode == SET_PACKET_PARAMS and len(frame) >= 5:
            # For this modem the fourth parameter is how long the packet is:
            # preamble takes two, then the header type, then the length.
            self.preamble = (frame[1] << 8) | frame[2]
            self.header_implicit = frame[3] == HEADER_IMPLICIT
            self.payload_length = frame[4]
        elif opcode == SET_TX_PARAMS and len(frame) >= 2:
            # Signed: the part goes below a milliwatt as well as above it.
            self.transmit_dbm = float(frame[1] - 256 if frame[1] > 127 else frame[1])
        elif opcode == SET_MODULATION_PARAMS and len(frame) >= 5:
            self.spreading = frame[1]
            self.bandwidth_code = frame[2]
            self.coding_rate = frame[3]
            self.low_data_rate = bool(frame[4])
        elif opcode in (GET_STATUS, GET_IRQ_STATUS, GET_RX_BUFFER_STATUS,
                        GET_PACKET_STATUS, GET_DEVICE_ERRORS, GET_PACKET_TYPE,
                        READ_REGISTER, READ_BUFFER):
            pass                                # answered while it was clocked
        elif opcode in (CALIBRATE_IMAGE,
                        SET_REGULATOR_MODE, SET_DIO2_AS_RF_SWITCH,
                        SET_DIO3_AS_TCXO,
                        SET_LORA_SYMB_TIMEOUT, STOP_TIMER_ON_PREAMBLE,
                        SET_RX_DUTY_CYCLE, CLEAR_DEVICE_ERRORS, GET_RSSI_INST,
                        SET_PA_CONFIG):
            pass                                # accepted; nothing to model yet
        elif opcode != 0x00:                    # a lone zero is the host idling
            if opcode not in self.unknown:
                self.unknown.add(opcode)
                log.debug("sx1262: opcode 0x%02x not modelled (%s)",
                          opcode, frame[:8].hex())

    def __repr__(self) -> str:
        return (f"<{self.name} mode={self.mode} commands={self.commands} "
                f"frequency={self.frequency} sent={self.sent} "
                f"heard={self.heard} collisions={self.collisions}>")
