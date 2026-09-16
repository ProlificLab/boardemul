#!/usr/bin/env python3
"""The guest boots with a translation thread per core.

`thread=multi` was unusable until ADR 0066. Upstream's IOMMU hook performed a
TLB flush on the calling thread for every CPU's notifier, so the core writing
the cache's MMU registers cleared the *other* core's translation table while
that core was running, and the emulator died about once in a hundred boots.

One boot cannot measure a rate like that and this test does not pretend to --
ADR 0066 carries the measurement, and `logs/`-side reproduction is what
produced it. What a single boot can hold is the thing a rate cannot: that the
configuration works at all. Nothing else in this suite runs two threads, the
default is one (ADR 0041), and the WPA2 handshake needs two (ADR 0064) -- so
without this the only supported use of `multi` would be untested.

    python3 -m pytest tests/test_tcg_multi.py -m slow
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_MESHTASTIC, MESHTASTIC_LITTLEFS, qemu_available, run_emulator,
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (FIRMWARE_MESHTASTIC.exists() and MESHTASTIC_LITTLEFS.exists()),
                       reason="Meshtastic firmware or its filesystem image not present"),
]

# The same mark the Meshtastic boot tests wait for: the firmware has mounted
# its filesystem and gone looking for its preferences, which is far enough in
# to have executed from every memory the cache remaps.
REACHED = "/littlefs/prefs/"


def test_the_firmware_boots_on_two_translation_threads(monkeypatch) -> None:
    """Runs in a local scratch directory rather than pytest's `tmp_path`.

    `pytest.ini` puts that under `logs/`, which on this host is a Windows bind
    mount, and a board cannot even start there: the GPIO peripheral binds a
    Unix socket in its output directory and the mount answers `Operation not
    supported`. `test_wifi_connect.py` avoids the same directory for a
    timing reason; this one would not run at all.
    """
    if not qemu_available():
        pytest.skip("no qemu-system-xtensa; run tools/build_qemu.sh")
    monkeypatch.setenv("BOARDEMUL_TCG_THREADS", "multi")
    scratch = Path(tempfile.mkdtemp(prefix="boardemul-tcgmulti-"))
    try:
        run = run_emulator(scratch, firmware=FIRMWARE_MESHTASTIC, board="tdeck",
                           timeout=240,
                           overlays=(f"0xc90000={MESHTASTIC_LITTLEFS}",),
                           extra=("--psram-octal",),
                           until=lambda uart, frames: REACHED in uart)
        # A crashed emulator and a stalled guest look the same from here --
        # both are a UART that stopped early -- so the message says how to
        # tell them apart rather than leaving the reader to guess.
        assert REACHED in run.uart, (
            "the guest did not reach its preferences on two translation "
            f"threads; {len(run.uart)} bytes of UART. If it died rather than "
            "stalled, the stderr log in the run directory names the signal."
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
