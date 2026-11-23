#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""What an I2C device on this board has to implement.

The controller itself moved into QEMU (ADR 0011): the driver builds one
transaction out of roughly twenty-four register accesses, and forwarding those
individually was the whole of the remaining bridge traffic. What crosses the
boundary now is a completed transaction, so all that is needed on this side is
the device behaviour.
"""

from __future__ import annotations

import logging

log = logging.getLogger("i2c.dev")


class I2CDevice:
    """A device on the bus. `write` returns False to NACK."""

    address: int = -1

    def start(self, reading: bool) -> None:
        """Called when the device is addressed, before any data moves."""

    def write(self, data: bytes) -> bool:
        return True

    def read(self, count: int) -> bytes:
        return bytes([0xFF]) * count

    def stop(self) -> None:
        """Called at the STOP condition."""
