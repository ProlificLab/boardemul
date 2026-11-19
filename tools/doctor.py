#!/usr/bin/env python3
"""Say what this machine lacks to run BoardEmul, and what fixes each (ADR 0147).

    python tools/doctor.py

Checks Python, where a run will start and what that needs (Docker, its
daemon and the build image, for a run in the container), the QEMU binary,
the pinned firmware and the probes. A missing image or probe is a warning:
a run needs only the image it boots. Anything a run cannot start without is
an error, and the exit status is 1 when there is one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OK, WARN, ERROR = "ok", "warn", "error"
RUNTIME_IMAGE = "boardemul-runtime"
# The label the runtime image carries: the digest of the sources its QEMU was
# built from, so a clone that has moved on can tell (ADR 0159).
SOURCES_LABEL = "org.boardemul.sources"
PROBES = ("restart_probe", "matrix_reset_probe", "cpenable_probe", "audio_probe",
          "serial_probe")


@dataclass
class Finding:
    level: str
    what: str
    detail: str = ""
    fix: str = ""


def _run(cmd: list[str], timeout: float = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_python() -> Finding:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 11):
        return Finding(ERROR, "Python", f"{version}; the tools read TOML with "
                       "tomllib, which is 3.11 and later", "install Python 3.12")
    return Finding(OK, "Python", version)


def sources_digest() -> str:
    """The digest of what the runtime image's QEMU is built from.

    The build script, the Dockerfile and every file under qemu-patch/, by
    path and content, with line endings made alike so that a Windows
    checkout and a Linux one agree."""
    paths = [REPO_ROOT / "tools" / "build_qemu.sh",
             REPO_ROOT / "docker" / "runtime" / "Dockerfile"]
    paths += sorted(p for p in (REPO_ROOT / "qemu-patch").rglob("*") if p.is_file())
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            continue
        digest.update(path.relative_to(REPO_ROOT).as_posix().encode() + b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return digest.hexdigest()[:16]


def check_where(image: str) -> list[Finding]:
    from emu import device as device_folder
    if device_folder.runs_natively():
        return [Finding(OK, "Runs start", "on this host (ADR 0140)")]
    found = [Finding(OK, "Runs start", "in the build container (ADR 0140)")]
    if not shutil.which("docker"):
        return found + [Finding(ERROR, "Docker", "not on PATH",
                                "install Docker Desktop and start it")]
    info = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if info is None or info.returncode != 0:
        return found + [Finding(ERROR, "Docker", "its daemon does not answer",
                                "start Docker Desktop")]
    found.append(Finding(OK, "Docker", f"server {info.stdout.strip()}"))
    have = _run(["docker", "image", "inspect", "--format", "{{json .Config}}", image])
    config = {}
    if have is not None and have.returncode == 0:
        try:
            config = json.loads(have.stdout) or {}
        except ValueError:
            config = {}
    env = " ".join(config.get("Env") or [])
    built_from = (config.get("Labels") or {}).get(SOURCES_LABEL)
    if have is None or have.returncode != 0:
        build = ("-f docker/runtime/Dockerfile ." if image == RUNTIME_IMAGE
                 else ".devcontainer")
        found.append(Finding(ERROR, "Build image", f"{image} is not built",
                             f"docker build -t {image} {build}"))
    elif "BOARDEMUL_QEMU=" in env and built_from != sources_digest():
        # Its QEMU lacks what this clone's QEMU sources add since (ADR 0159).
        found.append(Finding(ERROR, "Build image", f"{image} was built from "
                             "other QEMU sources than this clone's",
                             "python boardemul.py, which rebuilds it"))
    elif "BOARDEMUL_QEMU=" in env:
        # The runtime image carries QEMU itself (ADR 0159).
        found.append(Finding(OK, "Build image", f"{image}, with QEMU inside"))
    else:
        found.append(Finding(OK, "Build image", image))
    return found


def qemu_in_image(findings: list[Finding]) -> bool:
    return any(f.what == "Build image" and "QEMU inside" in f.detail for f in findings)


def check_qemu() -> Finding:
    binary = REPO_ROOT / "third_party" / "qemu" / "build" / "qemu-system-xtensa"
    fix = ("docker run --rm -v \"%CD%\":/work -w /work boardemul-build "
           "bash -lc tools/build_qemu.sh" if os.name == "nt"
           else "tools/build_qemu.sh")
    if not binary.is_file():
        return Finding(ERROR, "QEMU", f"no {binary.relative_to(REPO_ROOT).as_posix()}", fix)
    if os.name == "nt":
        # A Linux program: it runs in the container, not here.
        return Finding(OK, "QEMU", "built (a Linux binary, run in the container)")
    machines = _run([str(binary), "-machine", "help"])
    if machines is None or "esp32s3" not in machines.stdout:
        return Finding(ERROR, "QEMU", "built, but it has no esp32s3 machine", fix)
    return Finding(OK, "QEMU", "built, with the esp32s3 machine")


def check_firmware() -> list[Finding]:
    from tools import fetch_firmware
    found = []
    for entry in fetch_firmware.load():
        status = fetch_firmware.state(entry)
        if status == "ok":
            found.append(Finding(OK, f"Firmware {entry['name']}", entry["path"]))
        elif status == "missing":
            found.append(Finding(WARN, f"Firmware {entry['name']}", "not fetched",
                                 f"python tools/fetch_firmware.py --only {entry['name']}"))
        else:
            found.append(Finding(ERROR, f"Firmware {entry['name']}",
                                 "present, but not the pinned image",
                                 f"remove firmware/{entry['path']} and fetch it again"))
    return found


def check_probes() -> list[Finding]:
    found = []
    for probe in PROBES:
        built = (REPO_ROOT / "firmware" / f"{probe}.bin").is_file()
        found.append(Finding(OK if built else WARN, f"Probe {probe}",
                             "built" if built else "not built (its slow test skips)",
                             "" if built else f"python tools/{probe}/build.py "
                             "(needs PlatformIO)"))
    return found


def diagnose() -> list[Finding]:
    from emu import device as device_folder
    image = os.environ.get("BOARDEMUL_IMAGE") or device_folder.DEFAULT_IMAGE
    found = [check_python()]
    if found[0].level == ERROR:
        return found
    where = check_where(image)
    qemu = (Finding(OK, "QEMU", "in the runtime image (ADR 0159)")
            if qemu_in_image(where) else check_qemu())
    return found + where + [qemu] + check_firmware() + check_probes()


def main(argv: list[str] | None = None) -> int:
    findings = diagnose()
    marks = {OK: "ok  ", WARN: "warn", ERROR: "FAIL"}
    for f in findings:
        print(f"[{marks[f.level]}] {f.what:32s} {f.detail}")
        if f.fix:
            print(f"       fix: {f.fix}")
    errors = sum(f.level == ERROR for f in findings)
    warnings = sum(f.level == WARN for f in findings)
    print(f"\n{errors} error(s), {warnings} warning(s)"
          + ("; a device can start" if not errors else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
