#!/usr/bin/env python3
"""The host on the USB OTG cable (ADR 0166), against a device written here.

The controller is QEMU's (qemu-patch/esp_usb_otg.c); what reaches Python is
packets. The device below answers on the cable as the controller and a CDC
device behind it do: control transfers on endpoint 0 with their data and
status stages, a bulk IN endpoint that writes and a bulk OUT endpoint that
takes a packet at a time. If the host and that device agree, the host
enumerates it, opens its port, reads what it writes and writes what is typed.
The firmware end, the controller under TinyUSB, is tests/test_usb_otg_probe.py.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.devices import usb_otg as u  # noqa: E402

DATA_OUT, DATA_IN, NOTIFY = 1, 2, 3

DEVICE = struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0xEF, 0x02, 0x01, 64,
                     0x303A, 0x1001, 0x0100, 1, 2, 3, 1)


def _config() -> bytes:
    body = (
        # CDC communication interface 0, its notification endpoint.
        bytes([9, 4, 0, 0, 1, 0x02, 0x02, 0x00, 0])
        + bytes([5, 0x24, 0x00, 0x20, 0x01])
        + bytes([7, 5, 0x80 | NOTIFY, 3, 8, 0, 16])
        # CDC data interface 1: bulk OUT and bulk IN.
        + bytes([9, 4, 1, 0, 2, 0x0A, 0x00, 0x00, 0])
        + bytes([7, 5, DATA_OUT, 2, 64, 0, 0])
        + bytes([7, 5, 0x80 | DATA_IN, 2, 64, 0, 0])
    )
    return struct.pack("<BBHBBBBB", 9, 2, 9 + len(body), 2, 1, 0, 0x80, 50) + body


CONFIG = _config()


class CdcDevice:
    """A CDC device behind the controller, as it looks from the cable."""

    def __init__(self, device: bytes = DEVICE, config: bytes = CONFIG):
        self.descriptors = {u.DESC_DEVICE: device, u.DESC_CONFIG: config}
        self.host: u.CdcHost | None = None
        self.queue: list[tuple] = []
        self.requests: list[tuple[int, int]] = []
        self.setup = b""
        self.address = 0
        self.configuration = 0
        self.line_coding = b""
        self.dtr = False
        self.received = bytearray()
        self.expecting_status = False

    # The host's messages, queued so that nothing re-enters it.
    def cable(self, kind: int, ep: int, data: bytes) -> None:
        self.queue.append((kind, ep, bytes(data)))

    def run(self) -> None:
        while self.queue:
            kind, ep, data = self.queue.pop(0)
            if kind == u.FROM_HOST_SETUP:
                self._setup(data)
            elif kind == u.FROM_HOST_OUT:
                self._out(ep, data)

    def _setup(self, setup: bytes) -> None:
        self.setup = setup
        kind, request, value, index, length = struct.unpack("<BBHHH", setup)
        self.requests.append((kind, request))
        if kind & 0x80:
            if request != u.GET_DESCRIPTOR:
                raise AssertionError(f"unexpected request {setup.hex()}")
            data = self.descriptors.get(value >> 8, b"")[:length]
            for pos in range(0, len(data), 64):
                self.host.in_packet(0, data[pos:pos + 64], 64)
            if len(data) % 64 == 0 and len(data) < length:
                self.host.in_packet(0, b"", 64)
            self.expecting_status = True
        elif length:
            pass                                  # its data stage is next
        else:
            if request == u.SET_ADDRESS:
                self.address = value
            elif request == u.SET_CONFIGURATION:
                self.configuration = value
            elif request == u.CDC_SET_CONTROL_LINE_STATE:
                self.dtr = bool(value & 1)
            self.host.in_packet(0, b"", 64)       # status

    def _out(self, ep: int, data: bytes) -> None:
        self.host.taken(ep)
        if ep == 0 and self.expecting_status:
            self.expecting_status = False
        elif ep == 0:
            if self.setup[1] == u.CDC_SET_LINE_CODING:
                self.line_coding = data
            self.host.in_packet(0, b"", 64)       # status
        elif ep == DATA_OUT:
            self.received += data

    def write(self, data: bytes) -> None:
        for pos in range(0, len(data), 64):
            self.host.in_packet(DATA_IN, data[pos:pos + 64], 64)


def _host(device: CdcDevice | None = None) -> tuple[u.CdcHost, CdcDevice, list[bytes]]:
    device = device or CdcDevice()
    seen: list[bytes] = []
    host = u.CdcHost(device.cable, sink=seen.append)
    device.host = host
    return host, device, seen


def _enumerated() -> tuple[u.CdcHost, CdcDevice, list[bytes]]:
    host, device, seen = _host()
    host.attached()
    host.ready()
    device.run()
    return host, device, seen


def test_the_host_enumerates_the_device_and_opens_its_port() -> None:
    host, device, _ = _enumerated()
    assert host.state == "configured", host.summary()
    assert device.address == u.HOST_ADDRESS
    assert device.configuration == 1
    assert device.line_coding == u.LINE_CODING
    assert device.dtr
    assert (host.function.data_in, host.function.data_out) == (DATA_IN, DATA_OUT)
    assert "303a:1001 configured" in host.summary()


def test_the_requests_come_in_a_hosts_order() -> None:
    _, device, _ = _enumerated()
    assert device.requests == [
        (0x80, u.GET_DESCRIPTOR), (0x00, u.SET_ADDRESS), (0x80, u.GET_DESCRIPTOR),
        (0x80, u.GET_DESCRIPTOR), (0x80, u.GET_DESCRIPTOR), (0x00, u.SET_CONFIGURATION),
        (0x21, u.CDC_SET_LINE_CODING), (0x21, u.CDC_SET_CONTROL_LINE_STATE)]


def test_what_the_device_writes_reaches_the_host() -> None:
    host, device, seen = _enumerated()
    device.write(b"x" * 150)
    assert [len(p) for p in seen] == [64, 64, 22]
    assert host.bytes_in == 150


def test_what_is_typed_reaches_the_device_a_packet_at_a_time() -> None:
    host, device, _ = _enumerated()
    host.queue_out(b"y" * 100)
    # One packet on the cable until the device takes it.
    sent = [m for m in device.queue if m[0] == u.FROM_HOST_OUT]
    assert [len(m[2]) for m in sent] == [64]
    device.run()
    assert bytes(device.received) == b"y" * 100
    assert host.bytes_out == 100


def test_what_is_typed_before_the_port_opens_waits_for_it() -> None:
    host, device, _ = _host()
    host.queue_out(b"early")
    host.attached()
    host.ready()
    device.run()
    assert bytes(device.received) == b"early"


def test_a_detach_forgets_the_device_and_a_reset_enumerates_it_again() -> None:
    host, device, _ = _enumerated()
    host.detached()
    assert host.state == "detached"
    assert host.summary().startswith("usb otg: stopped while detached")
    host.attached()
    host.ready()
    device.run()
    assert host.state == "configured"


def test_a_device_that_answers_nonsense_stops_enumeration_and_says_so() -> None:
    host, device, _ = _host(CdcDevice(device=b"\x12\x07" + bytes(16)))
    host.attached()
    host.ready()
    device.run()
    assert host.state == "stopped"
    assert "device descriptor" in host.summary()


def test_the_cable_frames_messages_both_ways() -> None:
    """Messages split anywhere on the stream are read whole, and the host's
    answers are framed the same way, each reaction closed by a DONE that the
    controller waits for (ADR 0115)."""
    ours, theirs = socket.socketpair()
    cable = u.OtgCable("unused.sock")
    thread = threading.Thread(target=cable.serve, args=(ours,), daemon=True)
    thread.start()
    stream = (u.HEAD.pack(u.TO_HOST_ATTACHED, 0, 0, 0)
              + u.HEAD.pack(u.TO_HOST_READY, 0, 0, 0))
    for i in range(len(stream)):
        theirs.sendall(stream[i:i + 1])
        time.sleep(0.001)
    theirs.settimeout(2)

    def message() -> tuple[int, int, bytes]:
        head = b""
        while len(head) < u.HEAD.size:
            head += theirs.recv(u.HEAD.size - len(head))
        kind, ep, _arg, length = u.HEAD.unpack(head)
        body = b""
        while len(body) < length:
            body += theirs.recv(length - len(body))
        return kind, ep, body

    replies = [message() for _ in range(3)]
    assert [kind for kind, _ep, _body in replies] == [
        u.FROM_HOST_DONE, u.FROM_HOST_SETUP, u.FROM_HOST_DONE]
    assert replies[1][2] == struct.pack("<BBHHH", 0x80, u.GET_DESCRIPTOR,
                                        u.DESC_DEVICE << 8, 0, 64)
    assert cable.summary().startswith("usb otg: stopped while control")
    theirs.close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_the_cable_asks_qemu_for_the_controller_on_its_socket() -> None:
    cable = u.OtgCable("/out/otg.sock")
    assert cable.qemu_args() == ["-chardev", "socket,id=otg,path=/out/otg.sock",
                                 "-device", "esp-usb-otg,chardev=otg,id=otg"]


def test_the_ports_log_grows_locally_when_the_runs_folder_is_shared(
        tmp_path, monkeypatch) -> None:
    """The cable's thread is the one QEMU's controller waits on; it writes
    a packet at a time, so not over 9P (ADR 0175)."""
    from emu import local_mirror
    out = tmp_path / "out"
    out.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text(f"x {out.resolve().as_posix()} 9p rw 0 0\n")
    monkeypatch.setattr(local_mirror, "MOUNTS", mounts)
    cable = u.OtgCable.logging_to(out / "otg.sock", out / "t.otg.log")
    assert cable.log_path == out / "t.otg.log"
    assert cable.live_path != cable.log_path, "written elsewhere"
    cable.host.sink(b"INFO  | ")
    cable.host.sink(b"booted\n")
    assert cable.live_path.read_bytes() == b"INFO  | booted\n"
    cable.close()
    assert cable.log_path.read_bytes() == b"INFO  | booted\n", "all of it, at the close"
    assert not cable.live_path.exists()


def test_on_local_disk_the_ports_log_is_written_in_place(tmp_path, monkeypatch) -> None:
    from emu import local_mirror
    mounts = tmp_path / "mounts"
    mounts.write_text("x /elsewhere 9p rw 0 0\n")
    monkeypatch.setattr(local_mirror, "MOUNTS", mounts)
    cable = u.OtgCable.logging_to(tmp_path / "otg.sock", tmp_path / "t.otg.log")
    assert cable.live_path == cable.log_path
    cable.host.sink(b"x")
    cable.close()
    assert cable.log_path.read_bytes() == b"x"
