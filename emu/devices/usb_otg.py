#!/usr/bin/env python3
"""The host at the other end of the ESP32-S3's USB OTG cable (ADR 0166).

The controller is a QEMU device, `esp-usb-otg` (qemu-patch/esp_usb_otg.c),
which serves its registers at QEMU's speed. What crosses to here is what
crosses a USB cable: packets, and the bus's states. Each message is a
six-octet head -- type, endpoint, a sixteen-bit argument, a sixteen-bit length
-- and its data:

    controller to host                     host to controller
    0x01 attached (left soft-disconnect)   0x81 SETUP, eight octets
    0x02 detached                          0x82 OUT packet on endpoint n
    0x03 ready: reset and recovered
    0x04 IN packet on n; argument = its maximum packet size
    0x05 the OUT packet on n was taken

The controller keeps the bus's times (debounce, reset, recovery) on the
guest's clock. The host does what a host does with a full-speed device once
the bus is ready: enumeration, the configuration, and for a CDC ACM function
the line coding and DTR, after which it reads the data IN endpoint and writes
the data OUT endpoint a packet at a time.
"""

from __future__ import annotations

import logging
import os
import selectors
import socket
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("usb")

# The cable (esp_usb_otg.c).
HEAD = struct.Struct("<BBHH")
TO_HOST_ATTACHED, TO_HOST_DETACHED, TO_HOST_READY = 0x01, 0x02, 0x03
TO_HOST_IN, TO_HOST_TAKEN = 0x04, 0x05
FROM_HOST_SETUP, FROM_HOST_OUT = 0x81, 0x82
PKT_MAX = 1024

# Standard requests and descriptor types (USB 2.0 chapter 9), CDC (PSTN).
GET_DESCRIPTOR, SET_ADDRESS, SET_CONFIGURATION = 6, 5, 9
DESC_DEVICE, DESC_CONFIG, DESC_INTERFACE, DESC_ENDPOINT = 1, 2, 4, 5
CDC_SET_LINE_CODING, CDC_SET_CONTROL_LINE_STATE = 0x20, 0x22
CLASS_CDC, CLASS_CDC_DATA = 0x02, 0x0A
HOST_ADDRESS = 1
# 115200 baud, one stop bit, no parity, eight data bits.
LINE_CODING = struct.pack("<IBBB", 115200, 0, 0, 8)


@dataclass
class _Control:
    """One control transfer the host makes: the SETUP packet, any data it sends,
    and what to do with what comes back."""
    setup: bytes
    out_data: bytes | None = None
    done: Callable[[bytes], None] | None = None
    label: str = ""

    @property
    def length(self) -> int:
        return struct.unpack_from("<H", self.setup, 6)[0]

    @property
    def device_to_host(self) -> bool:
        return bool(self.setup[0] & 0x80)


@dataclass
class CdcFunction:
    """What enumeration found: the CDC interface and its two data endpoints."""
    comm_interface: int = -1
    data_in: int = -1
    data_out: int = -1
    data_out_size: int = 64


class CdcHost:
    """The host on the cable: enumeration, then the CDC data endpoints.

    `send(kind, endpoint, data)` puts a message on the cable; `sink(data)`
    takes what the device sends on its CDC port."""

    def __init__(self, send: Callable[[int, int, bytes], None],
                 sink: Callable[[bytes], None] | None = None):
        self.send = send
        self.sink = sink or (lambda data: None)
        self.bytes_in = 0
        self.bytes_out = 0
        self.pending_out = bytearray()
        self.attaches = 0
        self.detached()

    # ------------------------------------------------------ bus states

    def detached(self) -> None:
        self.state = "detached"
        self.controls: list[_Control] = []
        self.current: _Control | None = None
        self.stage = ""
        self.data_in = bytearray()
        self.function = CdcFunction()
        self.device_descriptor = b""
        self.config_value = 1
        self.out_in_flight = False
        self.trouble = ""

    def attached(self) -> None:
        """The device left soft-disconnect; the controller debounces and resets."""
        self.detached()
        self.state = "attached"
        self.attaches += 1
        log.info("usb: device attached")

    def ready(self) -> None:
        """The bus is reset and the device recovered: enumerate it. A reset
        while enumerated starts again from the beginning, as a host's does."""
        self.detached()
        self.state = "control"
        self._get_descriptor(DESC_DEVICE, 64, self._device_short, "device descriptor")
        self._next_control()

    # -------------------------------------------------- control transfers

    def _queue(self, setup: bytes, out_data: bytes | None = None,
               done: Callable[[bytes], None] | None = None, label: str = "") -> None:
        self.controls.append(_Control(setup, out_data, done, label))

    def _get_descriptor(self, kind: int, length: int, done, label: str,
                        index: int = 0) -> None:
        self._queue(struct.pack("<BBHHH", 0x80, GET_DESCRIPTOR, (kind << 8) | index, 0,
                                length), done=done, label=label)

    def _next_control(self) -> None:
        if self.current is not None:
            return
        if not self.controls:
            if self.state == "control":
                self.state = "configured"
                log.info("usb: configured; CDC data IN %d, OUT %d",
                         self.function.data_in, self.function.data_out)
                self._send_pending()
            return
        self.current = self.controls.pop(0)
        self.data_in = bytearray()
        self.send(FROM_HOST_SETUP, 0, self.current.setup)
        if self.current.device_to_host and self.current.length:
            self.stage = "data_in"
        elif self.current.out_data:
            # The data stage follows the SETUP; the device NAKs it until its
            # endpoint 0 is ready, and the controller holds it until then.
            self.stage = "data_out_sent"
            self.send(FROM_HOST_OUT, 0, self.current.out_data)
        else:
            self.stage = "status_in"

    def _finish_control(self) -> None:
        done, data = self.current.done, bytes(self.data_in)
        self.current = None
        self.stage = ""
        if done is not None:
            done(data)
        self._next_control()

    def in_packet(self, n: int, data: bytes, mps: int) -> None:
        """A packet the device sent on IN endpoint n, whose size is mps."""
        if n == 0:
            if self.current is None:
                return
            if self.stage == "data_in":
                self.data_in += data
                if len(data) < (mps or 64) or len(self.data_in) >= self.current.length:
                    self.stage = "status_out_sent"
                    self.send(FROM_HOST_OUT, 0, b"")
            elif self.stage == "status_in" and not data:
                self._finish_control()
            return
        if n == self.function.data_in and data:
            self.bytes_in += len(data)
            self.sink(data)

    def taken(self, n: int) -> None:
        """The device took the OUT packet the host had for endpoint n."""
        if n == 0:
            if self.current is None:
                return
            if self.stage == "status_out_sent":
                self._finish_control()
            elif self.stage == "data_out_sent":
                self.stage = "status_in"
            return
        if n == self.function.data_out:
            self.out_in_flight = False
            self._send_pending()

    # ---------------------------------------------------- enumeration

    def _device_short(self, data: bytes) -> None:
        if len(data) < 8 or data[1] != DESC_DEVICE:
            return self._give_up(f"device descriptor {data.hex()}")
        self._queue(struct.pack("<BBHHH", 0x00, SET_ADDRESS, HOST_ADDRESS, 0, 0),
                    done=self._addressed, label="set address")

    def _addressed(self, _data: bytes) -> None:
        self._get_descriptor(DESC_DEVICE, 18, self._device_full, "device descriptor")

    def _device_full(self, data: bytes) -> None:
        if len(data) < 18:
            return self._give_up(f"device descriptor {data.hex()}")
        self.device_descriptor = data
        vid, pid = struct.unpack_from("<HH", data, 8)
        log.info("usb: device %04x:%04x, class %02x", vid, pid, data[4])
        self._get_descriptor(DESC_CONFIG, 9, self._config_short, "configuration")

    def _config_short(self, data: bytes) -> None:
        if len(data) < 9 or data[1] != DESC_CONFIG:
            return self._give_up(f"configuration {data.hex()}")
        total = struct.unpack_from("<H", data, 2)[0]
        self._get_descriptor(DESC_CONFIG, total, self._config_full, "configuration")

    def _config_full(self, data: bytes) -> None:
        function, interface_class = CdcFunction(), -1
        pos = 0
        while pos + 2 <= len(data) and data[pos] >= 2:
            length, kind = data[pos], data[pos + 1]
            body = data[pos:pos + length]
            if kind == DESC_CONFIG and length >= 6:
                self.config_value = body[5]
            elif kind == DESC_INTERFACE and length >= 9:
                interface, interface_class = body[2], body[5]
                if interface_class == CLASS_CDC and function.comm_interface < 0:
                    function.comm_interface = interface
            elif kind == DESC_ENDPOINT and length >= 7 and interface_class == CLASS_CDC_DATA:
                address, attributes = body[2], body[3]
                size = struct.unpack_from("<H", body, 4)[0] & 0x7FF
                if attributes & 0x3 == 2:              # bulk
                    if address & 0x80 and function.data_in < 0:
                        function.data_in = address & 0xF
                    elif not address & 0x80 and function.data_out < 0:
                        function.data_out = address & 0xF
                        function.data_out_size = size or 64
            pos += length
        self.function = function
        self._queue(struct.pack("<BBHHH", 0x00, SET_CONFIGURATION, self.config_value, 0, 0),
                    label="set configuration")
        if function.comm_interface >= 0:
            itf = function.comm_interface
            self._queue(struct.pack("<BBHHH", 0x21, CDC_SET_LINE_CODING, 0, itf,
                                    len(LINE_CODING)), out_data=LINE_CODING,
                        label="line coding")
            # DTR and RTS: a terminal has opened the port.
            self._queue(struct.pack("<BBHHH", 0x21, CDC_SET_CONTROL_LINE_STATE, 3, itf, 0),
                        label="control line state")
        else:
            log.info("usb: no CDC function in the configuration; reading nothing")

    def _give_up(self, why: str) -> None:
        self.trouble = why
        self.state = "stopped"
        self.controls.clear()
        log.warning("usb: enumeration stopped at an unexpected %s", why)

    # ----------------------------------------------------- CDC data OUT

    def queue_out(self, data: bytes) -> None:
        self.pending_out += data
        self._send_pending()

    def _send_pending(self) -> None:
        n = self.function.data_out
        if (self.state != "configured" or self.out_in_flight
                or not self.pending_out or n < 0):
            return
        chunk = bytes(self.pending_out[:self.function.data_out_size])
        del self.pending_out[:len(chunk)]
        self.out_in_flight = True
        self.bytes_out += len(chunk)
        self.send(FROM_HOST_OUT, n, chunk)

    def summary(self) -> str:
        if self.state == "detached" and not self.attaches:
            return "usb otg: no device attached"
        if self.state != "configured":
            return f"usb otg: stopped while {self.state}" + (
                f" ({self.trouble})" if self.trouble else "")
        vid, pid = (struct.unpack_from("<HH", self.device_descriptor, 8)
                    if len(self.device_descriptor) >= 12 else (0, 0))
        return (f"usb otg: {vid:04x}:{pid:04x} configured, CDC port read "
                f"{self.bytes_in} byte(s), sent {self.bytes_out}")


class OtgCable:
    """The cable: a socket QEMU's `esp-usb-otg` reaches through a character
    device, with the host on this end, serviced on its own thread."""

    def __init__(self, socket_path: str | Path,
                 sink: Callable[[bytes], None] | None = None):
        self.socket_path = str(socket_path)
        self._lock = threading.RLock()
        self._conn: socket.socket | None = None
        self._listener: socket.socket | None = None
        self._stop = threading.Event()
        self._log_handle = None
        self.log_path: Path | None = None
        self.host = CdcHost(self._send, sink)

    # ---------------------------------------------------- QEMU's side

    def qemu_args(self) -> list[str]:
        return ["-chardev", f"socket,id=otg,path={self.socket_path}",
                "-device", "esp-usb-otg,chardev=otg,id=otg"]

    # ----------------------------------------------------- the socket

    def start(self) -> None:
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(1)
        threading.Thread(target=self._accept, name="usb-otg", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._conn, self._listener):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._listener is not None and os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.close()

    def _accept(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self.serve(conn)

    def serve(self, conn: socket.socket) -> None:
        """Read the controller's messages from `conn` until it closes."""
        self._conn = conn
        sel = selectors.DefaultSelector()
        sel.register(conn, selectors.EVENT_READ)
        pending = bytearray()
        while not self._stop.is_set():
            if not sel.select(0.25):
                continue
            try:
                data = conn.recv(65536)
            except OSError:
                break
            if not data:
                break
            pending += data
            while len(pending) >= HEAD.size:
                kind, ep, arg, length = HEAD.unpack_from(pending)
                if len(pending) < HEAD.size + length:
                    break
                body = bytes(pending[HEAD.size:HEAD.size + length])
                del pending[:HEAD.size + length]
                try:
                    self.dispatch(kind, ep, arg, body)
                except Exception:
                    # A host bug must not take the guest's port down.
                    log.exception("usb: the host raised on message 0x%02x", kind)
        self._conn = None

    def _send(self, kind: int, ep: int, data: bytes) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            conn.sendall(HEAD.pack(kind, ep, 0, len(data)) + data)
        except OSError:
            pass

    def dispatch(self, kind: int, ep: int, arg: int, data: bytes) -> None:
        with self._lock:
            if kind == TO_HOST_ATTACHED:
                self.host.attached()
            elif kind == TO_HOST_DETACHED:
                self.host.detached()
            elif kind == TO_HOST_READY:
                self.host.ready()
            elif kind == TO_HOST_IN:
                self.host.in_packet(ep, data, arg)
            elif kind == TO_HOST_TAKEN:
                self.host.taken(ep)
            else:
                log.warning("usb: the controller sent message 0x%02x", kind)

    # ---------------------------------------------------- from outside

    def type_bytes(self, data: bytes) -> None:
        """Bytes for the device's CDC port, as a terminal sends them."""
        with self._lock:
            self.host.queue_out(data)

    def summary(self) -> str:
        with self._lock:
            return self.host.summary()

    @property
    def bytes_in(self) -> int:
        return self.host.bytes_in

    # ------------------------------------------------------ on a board

    @classmethod
    def logging_to(cls, socket_path, path) -> "OtgCable":
        """The cable of a run: what the device sends on its CDC port is
        appended to `path`, the port's log beside the UART's and the USB
        Serial/JTAG port's (ADR 0166)."""
        path = Path(path)
        path.write_bytes(b"")
        handle = open(path, "ab", buffering=0)
        cable = cls(socket_path, sink=handle.write)
        cable._log_handle = handle
        cable.log_path = path
        return cable

    def close(self) -> None:
        handle = self._log_handle
        if handle is not None:
            with self._lock:
                self.host.sink = lambda data: None
                handle.close()
                self._log_handle = None
