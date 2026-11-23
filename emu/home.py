# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Where a user's devices, firmware, logs and builds are kept (ADR 0183).

In a clone -- the code with docker/runtime/Dockerfile beside it -- the
clone itself, as it always was. Installed, ~/.boardemul. BOARDEMUL_HOME
names another, either way.
"""

from __future__ import annotations

import os
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent


def is_clone() -> bool:
    """Whether the code runs from a clone, whose sources build its image."""
    return (CODE_ROOT / "docker" / "runtime" / "Dockerfile").is_file()


def home() -> Path:
    explicit = os.environ.get("BOARDEMUL_HOME")
    if explicit:
        return Path(explicit)
    return CODE_ROOT if is_clone() else Path.home() / ".boardemul"


def devices() -> Path:
    return home() / "devices"


def firmware() -> Path:
    return home() / "firmware"


def logs() -> Path:
    return home() / "logs"


def builds() -> Path:
    return home() / "build"
