# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""BoardEmul: run and test ESP32-S3 firmware without the board.

`python -m boardemul` (or `boardemul`) opens the page; `boardemul test`
runs a build and a test command (ADR 0182). From Python, a `Lab` makes and
drives devices.
"""

from boardemul.build import Build, BuildError
from boardemul.lab import Device, Lab, LabError, Screen, Serial, Shot, Timeout

__all__ = ["Build", "BuildError", "Device", "Lab", "LabError", "Screen", "Serial",
           "Shot", "Timeout"]
