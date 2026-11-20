#!/usr/bin/env python3
"""emu/local_mirror.py (ADR 0174): which folders are shared from the host, and
that what QEMU writes locally reaches the run's folder, as it grows and in
full at the end."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import local_mirror as lm  # noqa: E402

# A Docker Desktop container on Windows, as /proc/mounts reads in it.
MOUNTS = """overlay / overlay rw,relatime,lowerdir=/x 0 0
C:\\134 /work 9p rw,noatime,aname=drvfs;path=C:\\;uid=0 0 0
tmpfs /dev tmpfs rw,nosuid 0 0
"""


def test_a_folder_shared_from_the_host_is_told_from_local_disk() -> None:
    assert lm.filesystem_of(Path("/work/logs/devices/n/3"), MOUNTS) == "9p"
    assert lm.shared(Path("/work/logs"), MOUNTS)
    assert not lm.shared(Path("/tmp/run"), MOUNTS)              # the overlay
    assert not lm.shared(Path("/workbench"), MOUNTS), "a prefix is not a mount"


def test_on_local_disk_nothing_moves(tmp_path) -> None:
    target = tmp_path / "t.uart.log"
    mirror = lm.Mirror([target], mounts=MOUNTS.replace("/work", str(tmp_path / "other")))
    assert not mirror.active
    assert mirror.local(target) == target
    mirror.start()
    mirror.stop()


def test_what_is_written_locally_reaches_the_shared_folder(tmp_path) -> None:
    shared_folder = tmp_path / "out"
    shared_folder.mkdir()
    mounts = f"x {shared_folder.resolve().as_posix()} 9p rw 0 0\n"
    log, uart = shared_folder / "t.qemu.log", shared_folder / "t.uart.log"
    mirror = lm.Mirror([log, uart], period_s=0.05, mounts=mounts)
    assert mirror.active and mirror.local(log) != log
    mirror.start()
    try:
        with mirror.local(log).open("ab") as fh:
            fh.write(b"line one\n")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and log.read_bytes() != b"line one\n":
            time.sleep(0.02)
        assert log.read_bytes() == b"line one\n", "copied as it grows"
        with mirror.local(log).open("ab") as fh:
            fh.write(b"line two\n")
        mirror.local(uart).write_bytes(b"boot\n")
    finally:
        mirror.stop()
    assert log.read_bytes() == b"line one\nline two\n"
    assert uart.read_bytes() == b"boot\n"
    assert not mirror.folder.exists(), "the stand-ins are removed"
