# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Scenarios (ADR 0185): read strictly, played in order on the library, and
stopped at the first step that does not pass."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boardemul import scenario  # noqa: E402
from boardemul.lab import Shot, Timeout  # noqa: E402
from boardemul.scenario import ScenarioError, parse  # noqa: E402
from emu.png import RGB, encode_png  # noqa: E402

RED = encode_png(2, 2, bytes([255, 0, 0] * 4), colour=RGB)
BLUE = encode_png(2, 2, bytes([0, 0, 255] * 4), colour=RGB)


@pytest.fixture()
def folder(tmp_path: Path) -> Path:
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "firmware.bin").write_bytes(b"app")
    (tmp_path / "red.png").write_bytes(RED)
    return tmp_path


def read(folder: Path, text: str):
    return parse(text, folder / "s.toml")


def refused(folder: Path, text: str) -> str:
    with pytest.raises(ScenarioError) as caught:
        read(folder, text)
    return str(caught.value)


ONE = '[[device]]\nbuild = "build"\n'


def test_a_scenario_reads_its_devices_and_steps(folder):
    s = read(folder, ONE + '[[step]]\nexpect = "ready"\nport = "usb"\ntimeout = 5\n'
                           '[[step]]\ntap = [10, 20]\n[[step]]\nscreen = "red.png"\n')
    assert [d.name for d in s.devices] == ["dut"]
    assert s.devices[0].build == folder / "build"
    assert [(t.action, t.device) for t in s.steps] == [
        ("expect", "dut"), ("tap", "dut"), ("screen", "dut")]
    assert s.steps[0].options == {"port": "usb", "timeout": 5}
    assert s.steps[2].value == folder / "red.png"


def test_a_described_board_is_found_beside_the_scenario(folder):
    s = read(folder, '[[device]]\nbuild = "build"\nboard = "mine.toml"\n'
                     '[[step]]\nwait = 1\n')
    assert s.devices[0].board == str(folder / "mine.toml")


@pytest.mark.parametrize("text, says", [
    ('[[step]]\nwait = 1\n', "no [[device]]"),
    (ONE, "no [[step]]"),
    (ONE + '[[step]]\nwait = 1\ntap = [1, 2]\n', "does wait and tap"),
    (ONE + '[[step]]\nport = "usb"\n', "does nothing"),
    (ONE + '[[step]]\ntap = [1, 2]\ntimeout = 3\n', "'timeout'"),
    (ONE + '[[step]]\ntap = [1]\n', "[x, y]"),
    (ONE + '[[step]]\nexpect = "x"\nport = "spi"\n', "`port` is one of"),
    (ONE + '[[step]]\npixel = [1, 1]\n', "needs `rgb`"),
    (ONE + '[[step]]\nscreen = "absent.png"\n', "no picture at"),
    (ONE + '[[step]]\nwait = 1\ndevice = "other"\n', "no device 'other'"),
    ('[[device]]\nname = "a"\nbuild = "build"\n[[device]]\nname = "b"\nbuild = "build"\n'
     '[[step]]\nwait = 1\n', "needs `device`"),
    ('[[device]]\nname = "a"\nbuild = "build"\n[[device]]\nname = "a"\nbuild = "build"\n'
     '[[step]]\nwait = 1\n', "two devices are called 'a'"),
    ('[[device]]\nbuild = "build"\ncolour = 1\n[[step]]\nwait = 1\n', "'colour'"),
    ('title = "x"\n' + ONE + '[[step]]\nwait = 1\n', "'title'"),
    ('[[device]\n', "not TOML"),
])
def test_a_wrong_scenario_is_refused_and_says_why(folder, text, says):
    message = refused(folder, text)
    assert message.startswith(str(folder / "s.toml") + ": ")
    assert says in message


class FakeSerial:
    def __init__(self, lines):
        self.lines, self.sent = lines, []

    def expect(self, pattern, timeout):
        for line in self.lines:
            if re.search(pattern, line):
                return line
        raise Timeout(f"no line matching {pattern!r}")

    def send(self, text):
        self.sent.append(text)


class FakeScreen:
    def __init__(self, frames):
        self.frames = frames

    def wait(self, predicate, timeout):
        for png in self.frames:
            shot = Shot(png)
            if predicate(shot):
                return shot
        raise Timeout("no frame as wanted")


class FakeDevice:
    def __init__(self, name, log):
        self.name, self.log = name, log
        self.ports = {"uart": FakeSerial(["boot", "setup done"])}
        self.screen = FakeScreen([BLUE, RED])

    def start(self, air=None):
        self.log.append(("start", self.name, air))

    def stop(self):
        self.log.append(("stop", self.name))

    def serial(self, port="uart"):
        return self.ports.setdefault(port, FakeSerial([]))

    def tap(self, x, y, hold=0.3):
        self.log.append(("tap", self.name, x, y))

    def key(self, name, hold=0.15):
        self.log.append(("key", self.name, name))

    def type(self, text):
        self.log.append(("type", self.name, text))

    def save_logs(self, path):
        Path(path).write_bytes(b"zip")
        return Path(path)


class FakeLab:
    def __init__(self):
        self.log = []

    def device(self, name, build, board="tdeck", settings=()):
        self.log.append(("made", name, board, settings))
        return FakeDevice(name, self.log)


def test_a_scenario_plays_its_steps_in_order(folder):
    s = read(folder, '[[device]]\nname = "a"\nbuild = "build"\n'
                     '[[device]]\nname = "b"\nbuild = "build"\nsettings = ["--host-clock"]\n'
                     '[[step]]\ndevice = "a"\nexpect = "setup d"\n'
                     '[[step]]\ndevice = "b"\ntap = [3, 4]\n'
                     '[[step]]\ndevice = "a"\nscreen = "red.png"\n'
                     '[[step]]\ndevice = "b"\npixel = [0, 0]\nrgb = [255, 0, 0]\n'
                     '[[step]]\ndevice = "a"\nsave_screen = "out/last.png"\n'
                     '[[step]]\ndevice = "b"\ntype = "hi"\n')
    lab, said = FakeLab(), []
    assert scenario.run(s, lab, folder / "logs", out=said.append)
    assert lab.log[:2] == [("made", "a", "tdeck", ()), ("made", "b", "tdeck", ("--host-clock",))]
    assert ("start", "a", scenario.AIR) in lab.log          # several share an air
    assert ("tap", "b", 3, 4) in lab.log and ("type", "b", "hi") in lab.log
    assert (folder / "out" / "last.png").read_bytes() == BLUE
    assert [line[:4] for line in said[1:]] == ["ok  "] * 6
    assert {p.name for p in (folder / "logs").iterdir()} == {"a.zip", "b.zip"}
    assert lab.log[-2:] in ([("stop", "a"), ("stop", "b")],
                            [("stop", "b"), ("stop", "a")])


def test_a_scenario_stops_at_the_first_failing_step(folder):
    s = read(folder, ONE + '[[step]]\nexpect = "never said"\n[[step]]\ntap = [1, 1]\n')
    lab, said = FakeLab(), []
    assert not scenario.run(s, lab, out=said.append)
    assert said[-1].startswith("FAIL step 1, dut: expect 'never said'")
    assert not any(entry[0] == "tap" for entry in lab.log)
    assert ("stop", "dut") in lab.log
    assert ("start", "dut", None) in lab.log                # alone, no air


def test_the_command_says_a_wrong_file_before_starting_anything(folder, capsys):
    (folder / "bad.toml").write_text('[[step]]\nwait = 1\n')
    assert scenario.main([str(folder / "bad.toml")]) == 2
    assert "no [[device]]" in capsys.readouterr().err
