#!/usr/bin/env python3
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
"""

from __future__ import annotations

import logging
import math
import time

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
IRQ_CAD_DONE = 0x0080
IRQ_CAD_DETECTED = 0x0040

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
                 obstruction_db: float = 0.0) -> None:
        # `air` carries frames to and from other boards; `signal` is how this
        # part nudges the processor, which it does on a line of its own.
        self.air = air
        self.signal = signal
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
        self.mode = MODE_STANDBY_RC
        self.irq = 0
        self.irq_mask = 0
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
        # a second one lands on top of it and neither survives. A reset ends
        # both, and with them the TxDone a transmission would have raised.
        self.tx_until = 0.0
        self.rx_until = 0.0
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
        if self.asleep:
            # The select falling is what wakes it (13.1.1). A cold start is
            # a power-on: what the chip was told before sleeping is gone.
            self.asleep = False
            if not self.sleep_warm:
                self._power_on()
        reply = bytearray()
        for byte in data:
            reply.append(self._reply_at(len(self.frame)))
            self.frame.append(byte)
        return bytes(reply) if duplex else None

    def on_deselect(self) -> None:
        self._busy = (0, 0)
        if self.frame and not self.in_reset:
            self._apply(bytes(self.frame))
        self.frame = bytearray()

    def on_reset(self, asserted: bool) -> None:
        """NRESET moved. Held low, the chip is in reset and BUSY is high
        (Table 8-3); released, it starts up and BUSY falls once it is in
        STBY_RC, a cold start's time later (ADR 0111)."""
        self._power_on()
        self.in_reset = asserted
        self._drive_line()
        if asserted:
            self._busy = (BUSY_HELD, BUSY_HELD)
        else:
            self._busy = (WAKE_COLD_US * 1000, 0)

    def busy(self) -> tuple[int, int]:
        owed, self._busy = self._busy, (0, 0)
        return owed

    def _switch(self, to: int) -> None:
        """Owe BUSY the time Table 8-2 gives for going from the mode the chip
        is in to `to`, and the TCXO's start-up on top when the clock was off.
        Then be in that mode."""
        us = SWITCH_US.get((self.mode, to), 0)
        if us and self.mode == MODE_STANDBY_RC and self.tcxo_us:
            us += self.tcxo_us
        self._busy = (int(us * 1000), 0)
        self.mode = to

    def pump(self) -> None:
        """Run the clock and take anything off the air.

        Called from the board's ticker rather than on bus traffic, because an
        idle guest makes almost none.
        """
        now = time.time()
        if self.tx_until and now >= self.tx_until:
            self.tx_until = 0.0
            self._raise(IRQ_TX_DONE)
        if self.air is None:
            return

        for frame in self.air.poll():
            decoded = lora.decode(frame)
            if decoded is None:
                continue
            frequency, sent_at, airtime = (decoded.frequency, decoded.sent_at,
                                           decoded.airtime)
            payload = decoded.payload

            if frequency != self.frequency:
                # Tuned elsewhere. Nothing about being on the same air makes
                # two radios on different channels hear each other.
                self.wrong_frequency += 1
                continue
            if self.tx_until:
                self.deaf += 1  # transmitting, and deaf while it does
                continue
            if self.mode != MODE_RX:
                self.not_listening += 1
                continue

            # Whether it arrives strongly enough to be decoded at all. The
            # distance is the scenario's, the rest is arithmetic (ADR 0038).
            distance = math.dist(self.position, decoded.origin)
            arrived = lora.received_dbm(
                decoded.power_dbm, distance, lora.register_to_hz(frequency),
                exponent=self.exponent,
                transmit_gain_dbi=decoded.gain_dbi,
                receive_gain_dbi=self.antenna_dbi,
                shadowing_db=self.obstruction_db)
            floor = lora.sensitivity_dbm(self.bandwidth_code, self.spreading)
            if arrived < floor:
                self.out_of_range += 1
                log.debug("sx1262: %.1f dBm at %.0f m is under %.1f dBm",
                          arrived, distance, floor)
                continue

            ends = sent_at + airtime
            if now < self.rx_until:
                # Another packet is still arriving. Two on top of each other
                # is what a collision is, and neither survives it.
                self.collisions += 1
                self.rx_until = max(self.rx_until, ends)
                self.received_length = 0
                continue
            self.rx_until = ends
            payload = payload[:BUFFER_SIZE]
            for offset, value in enumerate(payload):
                self.buffer[(self.rx_base + offset) % BUFFER_SIZE] = value
            self.received_length = len(payload)
            self.last_rssi_dbm = arrived
            self.last_snr_db = arrived - lora.noise_floor_dbm(self.bandwidth_code)
            self.heard += 1
            # One line per packet received: they are rare, and a reception is
            # the event a mesh run waits for (ADR 0098).
            log.info("sx1262: heard a %d-byte packet at %.1f dBm",
                     len(payload), arrived)
            self._raise(IRQ_RX_DONE)

    def _raise(self, flag: int) -> None:
        """Set a flag and put the line where the flags say it should be.

        A host that configured an interrupt waits on the line rather than
        polling, and never looks at the flags until the line moves.
        """
        self.irq |= flag
        self._drive_line()

    def _drive_line(self) -> None:
        if self.signal is not None:
            self.signal(bool(self.irq & self.irq_mask))

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

        It goes out at once, but the channel is held for as long as the packet
        actually takes, and the flag saying the transmission finished waits
        that long too. A host that is told instantly would key the radio again
        before the first packet had left.
        """
        if not self.payload_length:
            self._raise(IRQ_TX_DONE)
            return
        payload = bytes(self.buffer[(self.tx_base + offset) % BUFFER_SIZE]
                        for offset in range(self.payload_length))
        duration = self.airtime(len(payload))
        now = time.time()
        self.tx_until = now + duration
        if self.air is not None:
            self.air.send(lora.encode(self.frequency, now, duration, payload,
                                      self.transmit_dbm, *self.position,
                                      gain_dbi=self.antenna_dbi))
        self.sent += 1
        log.debug("sx1262: transmitting %d bytes for %.0f ms",
                  len(payload), duration * 1000)

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
            self.mode = MODE_STANDBY_RC
            self._busy = (BUSY_HELD, (WAKE_WARM_US if self.sleep_warm
                                      else WAKE_COLD_US) * 1000)
        elif opcode == SET_FS:
            self._switch(MODE_FS)
        elif opcode == SET_TX:
            self._switch(MODE_TX)
            self._transmit()
        elif opcode == SET_RX:
            self._switch(MODE_RX)
        elif opcode == SET_CAD:
            # Listening before talking. Answering that the sweep finished and
            # found nothing is what lets a host go on to transmit; saying
            # nothing at all leaves it waiting here for ever, which is exactly
            # where both firmwares stopped.
            self.mode = MODE_RX
            self.cad_sweeps += 1
            self._raise(IRQ_CAD_DONE)
        elif opcode == CALIBRATE:
            self._busy = (CALIBRATE_US * 1000, 0)
        elif opcode == SET_DIO3_AS_TCXO and len(frame) >= 5:
            self.tcxo_us = int.from_bytes(frame[2:5], "big") * TCXO_STEP_US
        elif opcode == CLEAR_IRQ_STATUS and len(frame) >= 3:
            self.irq &= ~((frame[1] << 8) | frame[2]) & 0xFFFF
            # The line follows the flags, so clearing them lowers it and the
            # next event is an edge rather than nothing.
            self._drive_line()
        elif opcode == SET_DIO_IRQ_PARAMS and len(frame) >= 3:
            self.irq_mask = (frame[1] << 8) | frame[2]
            self._drive_line()
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
                        SET_DIO3_AS_TCXO, SET_RX_TX_FALLBACK,
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
