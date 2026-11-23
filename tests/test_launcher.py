#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""One command starts BoardEmul (ADR 0159).

Fast: the launcher's decisions, with Docker, the network and the server
standing in.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul import launcher as boardemul  # noqa: E402
from emu import device as device_folder  # noqa: E402
from emu import web  # noqa: E402
from tools import doctor, fetch_firmware  # noqa: E402


@pytest.fixture()
def world(monkeypatch):
    """A container host with nothing built and one image not fetched."""
    state = {"image_built": False, "stale": False, "fetched": [], "served": [],
             "built": 0, "why": []}
    # Recorded first, so whatever the launcher sets is undone afterwards.
    monkeypatch.setenv("BOARDEMUL_IMAGE", "")
    monkeypatch.delenv("BOARDEMUL_IMAGE")
    monkeypatch.setattr(device_folder, "runs_natively", lambda: False)

    def diagnose():
        found = [doctor.Finding(doctor.OK, "Python", "3.12")]
        if not state["image_built"]:
            found.append(doctor.Finding(doctor.ERROR, "Build image",
                                        "boardemul-runtime is not built", "docker build"))
        elif state["stale"]:
            found.append(doctor.Finding(doctor.ERROR, "Build image",
                                        "boardemul-runtime was built from other QEMU "
                                        "sources than this clone's", "python boardemul.py"))
        return found

    monkeypatch.setattr(doctor, "diagnose", diagnose)
    monkeypatch.setattr(fetch_firmware, "load", lambda: [{"name": "mesh"}, {"name": "max"}])
    monkeypatch.setattr(fetch_firmware, "state",
                        lambda e: "ok" if e["name"] == "max" else "missing")
    monkeypatch.setattr(fetch_firmware, "main",
                        lambda argv: state["fetched"].append(argv) or 0)

    def build(why):
        state["built"] += 1
        state["why"].append(why)
        state["image_built"], state["stale"] = True, False
        return True

    monkeypatch.setattr(boardemul, "build_runtime_image", build)
    monkeypatch.setattr(web, "main", lambda argv: state["served"].append(argv) or 0)
    return state


def test_a_new_machine_gets_the_image_the_firmware_and_the_page(world, monkeypatch):
    import os
    assert boardemul.main(["--no-open"]) == 0
    assert world["built"] == 1
    assert world["fetched"] == [["--only", "mesh"]]
    assert world["served"] == [["--port", "8420", "--no-open"]]
    assert os.environ["BOARDEMUL_IMAGE"] == "boardemul-runtime"


def test_a_second_start_only_serves(world):
    world["image_built"] = True
    boardemul.main(["--no-open"])
    assert world["built"] == 0 and world["served"]


def test_what_it_cannot_fix_stops_it(world, monkeypatch):
    monkeypatch.setattr(doctor, "diagnose", lambda: [
        doctor.Finding(doctor.ERROR, "Docker", "its daemon does not answer",
                       "start Docker Desktop")])
    assert boardemul.main(["--no-open"]) == 1
    assert world["served"] == [] and world["built"] == 0


def test_an_image_built_from_older_sources_is_built_again(world):
    world["image_built"] = world["stale"] = True
    assert boardemul.main(["--no-open"]) == 0
    assert world["built"] == 1 and "changed" in world["why"][0]
    assert world["served"]


def _docker_answering(monkeypatch, config: str):
    """Docker on PATH, its daemon up, and `image inspect` printing CONFIG."""
    import shutil
    import subprocess
    monkeypatch.setattr(device_folder, "runs_natively", lambda: False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)

    def run(cmd, timeout=20):
        out = config if cmd[1:3] == ["image", "inspect"] else "27.0"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    monkeypatch.setattr(doctor, "_run", run)


def test_the_doctor_wants_a_runtime_image_built_from_these_sources(monkeypatch):
    import json
    label = {"Env": ["BOARDEMUL_QEMU=/usr/local/bin/qemu-system-xtensa"],
             "Labels": {doctor.SOURCES_LABEL: doctor.sources_digest()}}
    _docker_answering(monkeypatch, json.dumps(label))
    image = [f for f in doctor.check_where("boardemul-runtime") if f.what == "Build image"]
    assert image[0].level == doctor.OK and "QEMU inside" in image[0].detail

    label["Labels"][doctor.SOURCES_LABEL] = "0" * 16
    _docker_answering(monkeypatch, json.dumps(label))
    image = [f for f in doctor.check_where("boardemul-runtime") if f.what == "Build image"]
    assert image[0].level == doctor.ERROR and "other QEMU sources" in image[0].detail


def test_the_development_image_carries_no_label_and_needs_none(monkeypatch):
    import json
    _docker_answering(monkeypatch, json.dumps({"Env": ["PATH=/usr/bin"], "Labels": None}))
    image = [f for f in doctor.check_where("boardemul-build") if f.what == "Build image"]
    assert image[0].level == doctor.OK


def test_the_sources_digest_follows_the_patches_and_not_line_endings(tmp_path, monkeypatch):
    for name in ("tools/build_qemu.sh", "docker/runtime/Dockerfile",
                 "qemu-patch/esp_guest_clock.c"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"one\ntwo\n")
    monkeypatch.setattr(doctor, "REPO_ROOT", tmp_path)
    first = doctor.sources_digest()
    (tmp_path / "qemu-patch/esp_guest_clock.c").write_bytes(b"one\r\ntwo\r\n")
    assert doctor.sources_digest() == first
    (tmp_path / "qemu-patch/esp_guest_clock.c").write_bytes(b"one\ntwo\nthree\n")
    assert doctor.sources_digest() != first
