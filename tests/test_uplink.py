#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""The uplink's host end, against stand-ins on localhost (ADR 0097).

The real internet is not reproducible, so nothing here touches it: a UDP echo, a
TCP echo, a port nothing listens on, and the name `localhost` play the remote
side. What is checked is what the NAT promises -- a datagram out and its answer
back on the same flow, a TCP connection that opens only when the host connect
does and fails when it cannot, bytes both ways with the ends shut in order, and
a fast server held to the buffer until the guest takes what is waiting.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import uplink as uplink_mod  # noqa: E402
from emu.uplink import Uplink  # noqa: E402


@pytest.fixture
def up():
    u = Uplink()
    u.start()
    yield u
    u.stop()


def _wait(up: Uplink, want, timeout: float = 5.0) -> tuple:
    """The first event `want` accepts, waiting for it."""
    deadline = time.monotonic() + timeout
    seen = []
    while time.monotonic() < deadline:
        for event in up.events():
            seen.append(event)
            if want(event):
                return event
        time.sleep(0.01)
    raise AssertionError(f"no such event; saw {seen}")


def _take_all(up: Uplink, flow, count: int, timeout: float = 5.0) -> tuple[bytes, bool]:
    got, finished = b"", False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (len(got) < count or not finished):
        data, finished = up.tcp_take(flow, 1 << 20)
        got += data
        if len(got) >= count and finished:
            break
        time.sleep(0.01)
    return got, finished


def _udp_echo() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))

    def serve():
        try:
            while True:
                data, peer = sock.recvfrom(65535)
                sock.sendto(data.upper(), peer)
        except OSError:
            pass
    threading.Thread(target=serve, daemon=True).start()
    return sock, sock.getsockname()[1]


def _tcp_server(handler) -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def serve():
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            handler(conn)
    threading.Thread(target=serve, daemon=True).start()
    return listener, listener.getsockname()[1]


def _echo_until_eof(conn: socket.socket) -> None:
    while True:
        data = conn.recv(65535)
        if not data:
            return
        conn.sendall(data)


def test_a_datagram_goes_out_and_its_answer_comes_back_on_the_flow(up) -> None:
    server, port = _udp_echo()
    try:
        up.udp_send("flow-1", ("127.0.0.1", port), b"ping")
        event = _wait(up, lambda e: e[0] == "udp")
        assert event == ("udp", "flow-1", ("127.0.0.1", port), b"PING")
    finally:
        server.close()


def test_a_tcp_connection_carries_bytes_both_ways_and_ends_in_order(up) -> None:
    listener, port = _tcp_server(_echo_until_eof)
    try:
        up.tcp_open("c", ("127.0.0.1", port))
        assert _wait(up, lambda e: e[0].startswith("tcp")) == ("tcp-connected", "c")
        up.tcp_send("c", b"hello, ")
        up.tcp_send("c", b"world")
        up.tcp_shutdown("c")       # the guest's FIN; the echo then closes too
        data, finished = _take_all(up, "c", 12)
        assert data == b"hello, world"
        assert finished, "the remote end's close reaches the guest's side"
    finally:
        listener.close()


def test_a_connect_nothing_answers_fails(up) -> None:
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()                  # nothing listens on it now
    up.tcp_open("x", ("127.0.0.1", port))
    event = _wait(up, lambda e: e[0].startswith("tcp"))
    assert event[:2] == ("tcp-failed", "x")


def test_a_fast_server_is_held_to_the_buffer_until_the_guest_takes(up) -> None:
    """The host socket stops being read once TCP_BUFFER waits for the guest, and
    starts again when the guest takes some: nothing is lost, and memory does not
    grow with the server's speed."""
    blob = bytes(range(256)) * 1024                   # 256 KiB
    listener, port = _tcp_server(lambda c: c.sendall(blob))
    try:
        up.tcp_open("f", ("127.0.0.1", port))
        _wait(up, lambda e: e[0] == "tcp-connected")
        time.sleep(0.5)                               # let it fill
        with up._lock:
            waiting = len(up._inbound["f"].data)
        assert waiting <= uplink_mod.TCP_BUFFER + uplink_mod.RECV_CHUNK, waiting
        data, finished = _take_all(up, "f", len(blob), timeout=10)
        assert data == blob and finished
    finally:
        listener.close()


def test_a_name_is_looked_up_through_the_host(up) -> None:
    up.resolve("q1", "localhost")
    event = _wait(up, lambda e: e[0] == "resolved")
    assert event[:3] == ("resolved", "q1", "localhost")
    assert "127.0.0.1" in event[3]


def test_a_name_that_does_not_exist_is_not_given_an_address(up) -> None:
    """`.invalid` never resolves (RFC 2606). Whether the host says so outright
    (an empty list, NXDOMAIN) or cannot say (None), it is not an address."""
    up.resolve("q2", "no-such-host.invalid")
    event = _wait(up, lambda e: e[0] == "resolved", timeout=15)
    assert event[3] in ([], None)
