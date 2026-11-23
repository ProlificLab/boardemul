#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""ADR 0176: a trace window is read from a shadow in QEMU, which answers as
the Python model would, and every access still reaches the model."""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu.board import PhyGates, TraceRegion  # noqa: E402
from emu.bus import OP_READ, OP_READ_SEEN, OP_WRITE, REQUEST, RESPONSE, BusServer  # noqa: E402
from emu.devices.ledc import Ledc  # noqa: E402
from emu.peripherals.adc import SarAdc  # noqa: E402
from tests.support import REPO_ROOT, qemu_available  # noqa: E402

needs_unix = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                                reason="the bridge's socket is a Unix one")
QEMU = REPO_ROOT / "third_party" / "qemu" / "build" / "qemu-system-xtensa"
needs_qemu = pytest.mark.skipif(os.name != "posix" or not qemu_available(),
                                reason="needs the built QEMU, on Linux")


# -- which windows are shadowed -----------------------------------------------


def test_a_trace_window_is_shadowed() -> None:
    arg = TraceRegion("fe", 0x60006000).qemu_device_arg("/s")
    assert arg.endswith(",shadow=on")


def test_a_gated_page_carries_its_own_gates_only() -> None:
    arg = PhyGates("radio_unknown", 0x6000E000).qemu_device_arg("/s")
    assert arg.endswith(",shadow=on,gates=0x6000e04c:0x1000000;0x6000e050:0x7000000")
    fe = PhyGates("fe", 0x60006000).qemu_device_arg("/s")
    assert fe.endswith(",gates=0x60006174:0x10000"), "not the radio page's gates"


def test_gates_turned_off_are_not_shadowed_in(monkeypatch) -> None:
    monkeypatch.setenv("BOARDEMUL_PHY_GATES", "0")
    assert PhyGates("radio_unknown", 0x6000E000).qemu_device_arg("/s").endswith(",shadow=on")


def test_a_model_with_behaviour_is_not_shadowed() -> None:
    assert "shadow" not in Ledc("ledc", 0x60019000).qemu_device_arg("/s")
    assert "shadow" not in SarAdc("sens", 0x60008800, 0x400).qemu_device_arg("/s")

    class Counting(TraceRegion):
        def read(self, offset: int, size: int) -> int:
            return super().read(offset, size) + 1

    assert "shadow" not in Counting("x", 0x60042000).qemu_device_arg("/s"), \
        "a subclass that reads otherwise is answered by Python"


def test_shadows_can_be_turned_off(monkeypatch) -> None:
    monkeypatch.setenv("BOARDEMUL_SHADOW", "0")
    assert "shadow" not in TraceRegion("fe", 0x60006000).qemu_device_arg("/s")


# -- the model hears an answered read -----------------------------------------


@needs_unix
def test_an_answered_read_is_counted_and_gets_no_reply(tmp_path) -> None:
    region = TraceRegion("fe", 0x60006000)
    server = BusServer(tmp_path / "bus.sock", [region])
    server.start()
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(str(tmp_path / "bus.sock"))
        conn.sendall(REQUEST.pack(OP_WRITE, 4, 0, 0x60006010, 0x1234))
        conn.sendall(REQUEST.pack(OP_READ_SEEN, 4, 0, 0x60006010, 0x1234))
        conn.sendall(REQUEST.pack(OP_READ, 4, 0, 0x60006010, 0))
        conn.settimeout(5)
        reply = conn.recv(RESPONSE.size)
        assert RESPONSE.unpack(reply)[0] == 0x1234, "the next reply is the plain read's"
        conn.close()
    finally:
        server.stop()
    assert region.accesses == 3
    assert list(server.recent) == [(OP_WRITE, 0x60006010, 4, 0x1234),
                                   (OP_READ, 0x60006010, 4, 0x1234),
                                   (OP_READ, 0x60006010, 4, 0x1234)]


# -- QEMU answers as the model would ------------------------------------------


class Qtest:
    """QEMU's qtest protocol on stdio: one command, one reply line."""

    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)

    def ask(self, line: str) -> str:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        while True:
            reply = self.proc.stdout.readline()
            if not reply:
                raise RuntimeError("QEMU went away: " + self.proc.stderr.read())
            if reply.startswith(("OK", "FAIL", "ERR")):
                return reply.strip()

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=5)


ACCESSES = [  # (op, address, size, value), the kinds the PHY's start-up makes
    ("w", 0x6000E050, 4, 0x00000001),
    ("r", 0x6000E050, 4, 0),            # a gate: bits 26..24 forced
    ("r", 0x6000E04C, 4, 0),            # a gate never written
    ("w", 0x6000E004, 4, 0xDEADBEEF),
    ("r", 0x6000E004, 4, 0),
    ("w", 0x6000E004, 1, 0x5A),         # a byte replaces the whole entry
    ("r", 0x6000E004, 4, 0),
    ("w", 0x6000E009, 1, 0x77),         # its own offset, not part of +8
    ("r", 0x6000E008, 4, 0),
    ("r", 0x6000E009, 1, 0),
    ("r", 0x6000E100, 4, 0),            # never written
    ("w", 0x60042010, 2, 0xBEEF),       # a plain trace window
    ("r", 0x60042010, 2, 0),
]


@needs_unix
@needs_qemu
def test_qemu_answers_every_read_as_the_python_model_does(tmp_path) -> None:
    served = [PhyGates("radio_unknown", 0x6000E000), TraceRegion("bt_unknown", 0x60042000)]
    model = [PhyGates("radio_unknown", 0x6000E000), TraceRegion("bt_unknown", 0x60042000)]
    sock = tmp_path / "bus.sock"
    server = BusServer(sock, served)
    server.start()
    argv = [str(QEMU), "-machine", "esp32s3", "-accel", "qtest", "-qtest", "stdio",
            "-display", "none", "-nodefaults", "-monitor", "none", "-serial", "none"]
    for region in served:
        argv += ["-device", region.qemu_device_arg(str(sock))]
    qemu = Qtest(argv)
    heard = []          # what Python should have been told, in order
    try:
        for op, addr, size, value in ACCESSES:
            region = next(r for r in model if r.base <= addr < r.base + r.size)
            width = {1: "b", 2: "w", 4: "l"}[size]
            if op == "w":
                assert qemu.ask(f"write{width} 0x{addr:x} 0x{value:x}") == "OK"
                region.write(addr - region.base, value, size)
                heard.append((OP_WRITE, addr, size, value))
                continue
            got = int(qemu.ask(f"read{width} 0x{addr:x}").split()[1], 16)
            want = region.read(addr - region.base, size) & ((1 << (8 * size)) - 1)
            assert got == want, f"read{width} 0x{addr:x}: QEMU 0x{got:x}, model 0x{want:x}"
            heard.append((OP_READ, addr, size, got))
        # The tail leaves out the machine reset QEMU announces at start.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(server.recent) < len(heard):
            time.sleep(0.05)
    finally:
        qemu.close()
        server.stop()
    assert list(server.recent) == heard, "every access reached Python, with its value"
    assert [r.regs for r in served] == [r.regs for r in model], "and kept the model's copy"
