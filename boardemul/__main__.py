# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""`boardemul test ...` runs a build and a test (ADR 0182); anything else is
the launcher's (ADR 0159)."""

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["test"]:
        from boardemul.cli import test
        return test(argv[1:])
    from boardemul.launcher import main as launch
    return launch(argv)


if __name__ == "__main__":
    sys.exit(main())
