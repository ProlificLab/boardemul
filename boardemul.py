#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Start BoardEmul from a clone: `python boardemul.py` (ADR 0159). The
launcher itself is boardemul/launcher.py; this file keeps the command."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from boardemul.launcher import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
