#!/usr/bin/env python3
"""The chip's USB serial port is on every run, and its output is kept (ADR 0100).

Fast: these check the command line. The slow half, a firmware whose Serial is
that port printing through it, is in test_usb_serial_boot.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from emu import run  # noqa: E402


def _argv(**kwargs) -> list[str]:
    return run.build_argv(Path("q"), Path("f"), Path("e"), Path("u"), Path("l"),
                          traced=False, psram_mb=8, **kwargs)


def test_the_port_is_there_without_a_file() -> None:
    """It is silicon: a run that keeps nothing still has a host on it."""
    argv = _argv()
    assert argv[argv.index("-device") + 1] == "esp-usb-serial-jtag"


def test_what_the_host_hears_goes_to_the_named_file() -> None:
    argv = _argv(usb=Path("out/t.usb.log"))
    chardev = argv[argv.index("-chardev") + 1]
    assert chardev == "file,id=usbjtag,path=out/t.usb.log"
    assert "esp-usb-serial-jtag,chardev=usbjtag" in argv
