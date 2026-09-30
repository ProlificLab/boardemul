#!/usr/bin/env python3
"""The uplink: guest flows out through the host's own sockets (ADR 0097).

With `--wifi-uplink` the gateway routes what is not for the LAN to the real
internet. This is the host-side half of that NAT. It knows nothing of 802.11,
IP headers or TCP state -- the peer terminates the guest's TCP and builds every
packet. What this does is own the host sockets: one connected UDP socket per
guest UDP flow, one TCP socket per guest TCP connection, and name lookups
through the host's resolver.

Nothing here may make the guest wait. The peer runs inside a QEMU timer callback
(ADR 0064), and a connect or a lookup can take seconds, so every socket
operation happens on this module's own thread. The peer's calls only queue a
command and return. Whatever comes back is queued as an event that the peer
drains on its next tick and puts on the air.

Flows are opaque keys chosen by the peer. Addresses are `(ip, port)` tuples of
dotted strings and integers, as the socket module uses them.

Events, as tuples:
    ("udp", flow, (ip, port), payload)      a datagram from the remote end
    ("tcp-connected", flow)                 the host connect succeeded
    ("tcp-failed", flow, reason)            it did not; the guest gets a reset
    ("tcp-reset", flow, reason)             an open connection broke
    ("resolved", flow, name, ips)           ips: a list (empty = no such name),
                                            or None (the lookup itself failed)
TCP bytes from the remote end are pulled with `tcp_take`, not sent as events,
so the peer can take no more than the guest has room for.
"""

from __future__ import annotations

import errno
import logging
import queue
import selectors
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

log = logging.getLogger("uplink")

# Bytes from a remote TCP end that may wait for the guest before the host socket
# stops being read. A fast server fills this and then waits on the guest's window
# instead of on memory.
TCP_BUFFER = 64 * 1024
RECV_CHUNK = 16 * 1024
# A UDP flow with no traffic either way for this long is closed; a DNS or NTP
# exchange is over in well under a second.
UDP_IDLE_S = 60.0
# How long a host connect may take before the guest is told it failed.
CONNECT_TIMEOUT_S = 10.0
# The loop's longest sleep, which bounds how late an idle expiry or a connect
# timeout can be noticed. Commands wake it at once.
POLL_S = 0.25

_IN_PROGRESS = {0, errno.EINPROGRESS, errno.EWOULDBLOCK,
                getattr(errno, "WSAEWOULDBLOCK", 10035)}


@dataclass
class _Udp:
    sock: socket.socket
    dst: tuple[str, int]
    last: float


@dataclass
class _Inbound:
    """Bytes from a remote TCP end waiting for the peer, shared between the
    uplink's thread and the peer's under `Uplink._lock`."""
    data: bytearray = field(default_factory=bytearray)
    eof: bool = False


@dataclass
class _Tcp:
    sock: socket.socket
    dst: tuple[str, int]
    started: float
    connected: bool = False
    outbuf: bytearray = field(default_factory=bytearray)
    shutdown: bool = False           # the guest has finished sending
    shut: bool = False               # and that has been passed on
    mask: int = 0                    # what the selector is watching for


class Uplink:
    """The host end of the NAT. Start it, feed it commands, drain its events."""

    def __init__(self) -> None:
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._commands: queue.SimpleQueue = queue.SimpleQueue()
        self._events: queue.SimpleQueue = queue.SimpleQueue()
        self._udp: dict[object, _Udp] = {}
        self._tcp: dict[object, _Tcp] = {}
        self._inbound: dict[object, _Inbound] = {}
        self._lock = threading.Lock()
        self._resolver = ThreadPoolExecutor(max_workers=2,
                                            thread_name_prefix="uplink-dns")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._sel.register(self._wake_r, selectors.EVENT_READ)
        self._thread = threading.Thread(target=self._run, name="uplink",
                                        daemon=True)
        self._thread.start()
        log.info("uplink up: guest flows leave through the host's sockets")

    def stop(self) -> None:
        self._stop.set()
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._resolver.shutdown(wait=False, cancel_futures=True)
        for sock in ([u.sock for u in self._udp.values()]
                     + [t.sock for t in self._tcp.values()]
                     + [self._wake_r, self._wake_w]):
            try:
                sock.close()
            except OSError:
                pass
        self._sel.close()

    # -------------------------------------------------- the peer's commands

    def udp_send(self, flow: object, dst: tuple[str, int], payload: bytes) -> None:
        self._command(("udp-send", flow, dst, payload))

    def tcp_open(self, flow: object, dst: tuple[str, int]) -> None:
        with self._lock:
            self._inbound[flow] = _Inbound()
        self._command(("tcp-open", flow, dst))

    def tcp_send(self, flow: object, data: bytes) -> None:
        if data:
            self._command(("tcp-send", flow, data))

    def tcp_shutdown(self, flow: object) -> None:
        """The guest sent its FIN: stop sending once what it wrote has gone."""
        self._command(("tcp-shutdown", flow))

    def tcp_close(self, flow: object) -> None:
        """Forget the connection at once (the guest reset it, or it is over)."""
        self._command(("tcp-close", flow))

    def tcp_take(self, flow: object, limit: int) -> tuple[bytes, bool]:
        """Up to `limit` bytes from the remote end, and whether the remote end
        has finished and everything it sent has now been taken."""
        with self._lock:
            inbound = self._inbound.get(flow)
            if inbound is None:
                return b"", False
            was_full = len(inbound.data) >= TCP_BUFFER
            data = bytes(inbound.data[:max(0, limit)])
            del inbound.data[:len(data)]
            finished = inbound.eof and not inbound.data
            now_room = len(inbound.data) < TCP_BUFFER
        if was_full and now_room:
            self._command(("tcp-interest", flow))   # start reading it again
        return data, finished

    def resolve(self, flow: object, name: str) -> None:
        """Look `name` up through the host's resolver, off every thread that
        matters; the answer comes back as a "resolved" event."""
        self._resolver.submit(self._lookup, flow, name)

    def events(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # --------------------------------------------------------- the thread

    def _command(self, command: tuple) -> None:
        self._commands.put(command)
        self._wake()

    def _wake(self) -> None:
        try:
            self._wake_w.send(b"\0")
        except OSError:
            pass                            # a wake is already pending

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ready = self._sel.select(timeout=POLL_S)
            except OSError:
                break
            for key, mask in ready:
                if key.fileobj is self._wake_r:
                    try:
                        while self._wake_r.recv(4096):
                            pass
                    except OSError:
                        pass
                    continue
                kind, flow = key.data
                if kind == "udp":
                    self._udp_readable(flow)
                else:
                    self._tcp_ready(flow, mask)
            self._drain_commands()
            self._expire()

    def _drain_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            name, flow = command[0], command[1]
            try:
                if name == "udp-send":
                    self._udp_send(flow, command[2], command[3])
                elif name == "tcp-open":
                    self._tcp_open(flow, command[2])
                elif name == "tcp-send":
                    tcp = self._tcp.get(flow)
                    if tcp is not None:
                        tcp.outbuf += command[2]
                        self._interest(flow)
                elif name == "tcp-shutdown":
                    tcp = self._tcp.get(flow)
                    if tcp is not None:
                        tcp.shutdown = True
                        self._interest(flow)
                elif name == "tcp-close":
                    self._tcp_forget(flow)
                elif name == "tcp-interest":
                    self._interest(flow)
            except OSError as exc:
                log.info("uplink: %s for %r failed: %s", name, flow, exc)

    # ---------------------------------------------------------------- UDP

    def _udp_send(self, flow: object, dst: tuple[str, int], payload: bytes) -> None:
        udp = self._udp.get(flow)
        if udp is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            # Connected: the kernel then accepts datagrams from `dst` only, so
            # nothing unsolicited reaches the guest through this flow.
            sock.connect(dst)
            udp = self._udp[flow] = _Udp(sock, dst, time.monotonic())
            self._sel.register(sock, selectors.EVENT_READ, ("udp", flow))
        udp.last = time.monotonic()
        try:
            udp.sock.send(payload)
        except OSError as exc:
            log.info("uplink: udp to %s:%d not sent: %s", *dst, exc)

    def _udp_readable(self, flow: object) -> None:
        udp = self._udp.get(flow)
        if udp is None:
            return
        while True:
            try:
                data = udp.sock.recv(65535)
            except BlockingIOError:
                return
            except OSError:
                # An ICMP unreachable surfaces as an error on a connected UDP
                # socket. The guest would not have been told either; drop it.
                return
            udp.last = time.monotonic()
            self._events.put(("udp", flow, udp.dst, data))

    # ---------------------------------------------------------------- TCP

    def _tcp_open(self, flow: object, dst: tuple[str, int]) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        self._tcp[flow] = _Tcp(sock, dst, time.monotonic())
        err = sock.connect_ex(dst)
        if err not in _IN_PROGRESS:
            self._tcp_fail(flow, errno.errorcode.get(err, str(err)))
            return
        self._interest(flow)

    def _interest(self, flow: object) -> None:
        """Watch the socket for what the connection needs now: writable while
        connecting or while there is something to send (or a shutdown to pass
        on), readable while connected and the guest has room waiting."""
        tcp = self._tcp.get(flow)
        if tcp is None:
            return
        mask = 0
        if not tcp.connected or tcp.outbuf or (tcp.shutdown and not tcp.shut):
            mask |= selectors.EVENT_WRITE
        if tcp.connected:
            with self._lock:
                inbound = self._inbound.get(flow)
                room = (inbound is not None and not inbound.eof
                        and len(inbound.data) < TCP_BUFFER)
            if room:
                mask |= selectors.EVENT_READ
        if mask == tcp.mask:
            return
        if tcp.mask and mask:
            self._sel.modify(tcp.sock, mask, ("tcp", flow))
        elif mask:
            self._sel.register(tcp.sock, mask, ("tcp", flow))
        else:
            self._sel.unregister(tcp.sock)
        tcp.mask = mask

    def _tcp_ready(self, flow: object, mask: int) -> None:
        tcp = self._tcp.get(flow)
        if tcp is None:
            return
        if not tcp.connected:
            if mask & selectors.EVENT_WRITE:
                err = tcp.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err:
                    self._tcp_fail(flow, errno.errorcode.get(err, str(err)))
                    return
                tcp.connected = True
                self._events.put(("tcp-connected", flow))
                self._interest(flow)
            return
        try:
            if mask & selectors.EVENT_READ:
                self._tcp_read(flow, tcp)
            if mask & selectors.EVENT_WRITE and flow in self._tcp:
                self._tcp_write(tcp)
        except OSError as exc:
            self._events.put(("tcp-reset", flow, str(exc)))
            self._tcp_forget(flow)
            return
        self._interest(flow)

    def _tcp_read(self, flow: object, tcp: _Tcp) -> None:
        try:
            data = tcp.sock.recv(RECV_CHUNK)
        except BlockingIOError:
            return
        with self._lock:
            inbound = self._inbound.get(flow)
            if inbound is None:
                return
            if data:
                inbound.data += data
            else:
                inbound.eof = True           # the remote end has finished

    def _tcp_write(self, tcp: _Tcp) -> None:
        if tcp.outbuf:
            try:
                sent = tcp.sock.send(tcp.outbuf)
            except BlockingIOError:
                return
            del tcp.outbuf[:sent]
        if not tcp.outbuf and tcp.shutdown and not tcp.shut:
            tcp.sock.shutdown(socket.SHUT_WR)
            tcp.shut = True

    def _tcp_fail(self, flow: object, reason: str) -> None:
        self._events.put(("tcp-failed", flow, reason))
        self._tcp_forget(flow)

    def _tcp_forget(self, flow: object) -> None:
        tcp = self._tcp.pop(flow, None)
        with self._lock:
            self._inbound.pop(flow, None)
        if tcp is None:
            return
        if tcp.mask:
            try:
                self._sel.unregister(tcp.sock)
            except (KeyError, ValueError):
                pass
        try:
            tcp.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------- upkeep

    def _expire(self) -> None:
        now = time.monotonic()
        for flow, udp in list(self._udp.items()):
            if now - udp.last > UDP_IDLE_S:
                self._sel.unregister(udp.sock)
                udp.sock.close()
                del self._udp[flow]
        for flow, tcp in list(self._tcp.items()):
            if not tcp.connected and now - tcp.started > CONNECT_TIMEOUT_S:
                self._tcp_fail(flow, "timed out")

    # --------------------------------------------------------------- DNS

    def _lookup(self, flow: object, name: str) -> None:
        try:
            infos = socket.getaddrinfo(name, None, socket.AF_INET,
                                       socket.SOCK_STREAM)
        except socket.gaierror as exc:
            no_such = {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", None)}
            ips = [] if exc.errno in no_such else None
        except (OSError, UnicodeError):
            ips = None
        else:
            ips = []
            for info in infos:
                ip = info[4][0]
                if ip not in ips:
                    ips.append(ip)
        self._events.put(("resolved", flow, name, ips))
