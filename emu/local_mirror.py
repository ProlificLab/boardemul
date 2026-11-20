#!/usr/bin/env python3
"""Files QEMU writes during a run, kept on local disk where the run's folder
is shared from the host, and copied there as they grow (ADR 0174).

On Docker Desktop a folder shared from Windows is mounted over 9P: every
write is a round trip to the host's filesystem. QEMU writes its log a line
at a time, and the UART's and USB port's files as the guest writes them, all
from the thread that runs the guest, so the guest waited on Windows for every
line. Measured: Meshtastic's WiFi join took a hundred guest seconds with its
run's folder shared and seven with it local.

    mirror = Mirror([uart, usb, qemu_log])      # nothing to do on local disk
    qemu_uart = mirror.local(uart)               # where QEMU writes instead
    mirror.start(); ...; mirror.stop()          # copy as it grows, then the rest
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path

# Filesystems a folder shared from the host comes in on: Docker Desktop's on
# Windows (9p, virtiofs) and macOS (virtiofs, grpcfuse), and network mounts.
SHARED_FILESYSTEMS = {"9p", "virtiofs", "fuse.grpcfuse", "fakeowner", "fuse",
                      "fuse.sshfs", "cifs", "smb3", "nfs", "nfs4", "drvfs"}
MOUNTS = Path("/proc/mounts")


def filesystem_of(path: Path, mounts: str | None = None) -> str | None:
    """The type of the filesystem `path` lies on, by the longest mount point
    that contains it; None where there is no mount table to read."""
    if mounts is None:
        try:
            mounts = MOUNTS.read_text(errors="replace")
        except OSError:
            return None
    # Resolved where there is a mount table, POSIX; elsewhere, as given.
    target = (Path(path).resolve() if os.name == "posix" else Path(path)).as_posix()
    best, kind = "", None
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point = fields[1].replace("\\040", " ")
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and len(point) >= len(best):
            best, kind = point, fields[2]
    return kind


def shared(path: Path, mounts: str | None = None) -> bool:
    """Whether writing under `path` goes to another machine's filesystem."""
    return filesystem_of(path, mounts) in SHARED_FILESYSTEMS


class Mirror:
    """Local stand-ins for files in a shared folder, copied into it.

    Where the folder is not shared, every file is its own stand-in and
    nothing is copied: `local()` hands back the path it was given."""

    def __init__(self, targets: list[Path], period_s: float = 0.5,
                 mounts: str | None = None):
        self.period_s = period_s
        self.targets = [Path(t) for t in targets]
        self.active = bool(self.targets) and shared(self.targets[0].parent, mounts)
        self.folder = (Path(tempfile.mkdtemp(prefix="boardemul-run-"))
                       if self.active else None)
        self._copied = {t: 0 for t in self.targets}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def local(self, target: Path) -> Path:
        """Where to write `target` while the run lasts."""
        target = Path(target)
        if not self.active:
            return target
        return self.folder / target.name

    def start(self) -> None:
        if not self.active:
            return
        for target in self.targets:
            target.write_bytes(b"")
        self._thread = threading.Thread(target=self._run, name="mirror", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.period_s):
            self.copy()

    def copy(self) -> None:
        """What is new in each stand-in, appended to its target in one write."""
        if not self.active:
            return
        with self._lock:
            for target in self.targets:
                source = self.local(target)
                try:
                    with source.open("rb") as fh:
                        fh.seek(self._copied[target])
                        new = fh.read()
                except OSError:
                    continue
                if not new:
                    continue
                with target.open("ab") as out:
                    out.write(new)
                self._copied[target] += len(new)

    def stop(self) -> None:
        """Copy the rest, and remove the stand-ins. Call once QEMU has
        stopped writing."""
        if not self.active:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.copy()
        shutil.rmtree(self.folder, ignore_errors=True)
