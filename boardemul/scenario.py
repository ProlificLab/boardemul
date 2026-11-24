# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
"""Scenarios: devices and steps in a TOML file, run on the test library
(ADR 0185).

    boardemul scenario checks/boot.toml --logs boardemul-logs

`load()` reads a scenario strictly and raises `ScenarioError` naming what is
wrong; `run()` plays it on a `Lab` and answers whether every step passed,
saying each one as it goes.
"""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from boardemul.build import Build, BuildError
from boardemul.lab import SERIAL_PORTS, LabError, Shot

DEFAULT_TIMEOUT = 60.0
AIR = "boardemul-scenario"      # the LoRa air a scenario's devices share
# Each step's action, and the keys it may carry beside `device`.
ACTIONS: dict[str, tuple[str, ...]] = {
    "expect": ("port", "timeout"),
    "send": ("port",),
    "tap": ("hold",),
    "key": ("hold",),
    "type": (),
    "wait": (),
    "screen": ("tolerance", "timeout"),
    "pixel": ("rgb", "timeout"),
    "save_screen": ("timeout",),
}


class ScenarioError(ValueError):
    """What is wrong with a scenario file, said so its author can fix it."""


@dataclass
class DeviceSpec:
    name: str
    build: Path
    board: str = "tdeck"
    settings: tuple[str, ...] = ()


@dataclass
class Step:
    number: int
    action: str
    value: object
    device: str
    options: dict = field(default_factory=dict)

    def describe(self) -> str:
        value = self.value
        if self.action in ("expect", "send", "type"):
            value = repr(value)
        return f"{self.device}: {self.action} {value}"


@dataclass
class Scenario:
    path: Path
    devices: list[DeviceSpec]
    steps: list[Step]


def load(path: str | Path) -> Scenario:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"{path}: {exc.strerror or exc}") from None
    return parse(text, path)


def parse(text: str, path: Path) -> Scenario:
    where = str(path)

    def fail(message: str):
        raise ScenarioError(f"{where}: {message}")

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        fail(f"not TOML: {exc}")
    extra = sorted(set(data) - {"device", "step"})
    if extra:
        fail(f"{', '.join(map(repr, extra))}: a scenario has [[device]] and [[step]] only")
    raw_devices = data.get("device", [])
    raw_steps = data.get("step", [])
    if not isinstance(raw_devices, list) or not raw_devices:
        fail("no [[device]]: say which build runs, and on which board")
    if not isinstance(raw_steps, list) or not raw_steps:
        fail("no [[step]]: say what to do and what to wait for")

    base = path.parent
    devices: list[DeviceSpec] = []
    for n, entry in enumerate(raw_devices, 1):
        what = f"device {n}"
        extra = sorted(set(entry) - {"name", "build", "board", "settings"})
        if extra:
            fail(f"{what} has {', '.join(map(repr, extra))}; it takes name, "
                 "build, board and settings")
        name = entry.get("name", "dut" if len(raw_devices) == 1 else "")
        if not isinstance(name, str) or not name:
            fail(f"{what} needs a `name` when there are several")
        if any(d.name == name for d in devices):
            fail(f"two devices are called {name!r}")
        build = entry.get("build")
        if not isinstance(build, str):
            fail(f"{what} ({name}) needs `build`, its build folder")
        board = entry.get("board", "tdeck")
        if not isinstance(board, str):
            fail(f"{what} ({name}): `board` is tdeck, tdeck-max or a NAME.toml")
        if board.endswith(".toml"):
            board = str(base / board)
        settings = entry.get("settings", [])
        if not isinstance(settings, list) or not all(isinstance(s, str) for s in settings):
            fail(f"{what} ({name}): `settings` is a list of run settings")
        devices.append(DeviceSpec(name, base / build, board, tuple(settings)))

    names = [d.name for d in devices]
    steps: list[Step] = []
    for n, entry in enumerate(raw_steps, 1):
        what = f"step {n}"
        actions = [k for k in entry if k in ACTIONS]
        if len(actions) != 1:
            fail(f"{what} does {'nothing' if not actions else ' and '.join(actions)}; "
                 f"a step does one of {', '.join(ACTIONS)}")
        action = actions[0]
        allowed = {action, "device"} | set(ACTIONS[action])
        extra = sorted(set(entry) - allowed)
        if extra:
            fail(f"{what} ({action}) has {', '.join(map(repr, extra))}; it takes "
                 f"{', '.join(sorted(allowed))}")
        device = entry.get("device")
        if device is None:
            if len(devices) > 1:
                fail(f"{what} ({action}) needs `device`: one of {', '.join(names)}")
            device = names[0]
        elif device not in names:
            fail(f"{what}: no device {device!r}; there is {', '.join(names)}")
        value = entry[action]
        options = {k: entry[k] for k in ACTIONS[action] if k in entry}
        _check(fail, what, action, value, options, base)
        if action in ("screen", "save_screen"):
            value = base / value
        steps.append(Step(n, action, value, device, options))
    return Scenario(path, devices, steps)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(fail, what, action, value, options, base: Path) -> None:
    if action in ("expect", "send", "key", "type", "screen", "save_screen") \
            and not isinstance(value, str):
        fail(f"{what}: `{action}` is text")
    if action in ("tap", "pixel") and not (
            isinstance(value, list) and len(value) == 2 and all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value)):
        fail(f"{what}: `{action}` is [x, y], two whole numbers")
    if action == "wait" and not (_number(value) and value >= 0):
        fail(f"{what}: `wait` is a number of seconds")
    if action == "pixel":
        rgb = options.get("rgb")
        if not (isinstance(rgb, list) and len(rgb) == 3 and all(
                isinstance(v, int) and 0 <= v <= 255 for v in rgb)):
            fail(f"{what}: `pixel` needs `rgb`, three values from 0 to 255")
    if action == "screen" and not (base / value).is_file():
        fail(f"{what}: no picture at {base / value}")
    if "port" in options and options["port"] not in SERIAL_PORTS:
        fail(f"{what}: `port` is one of {', '.join(SERIAL_PORTS)}")
    for key in ("timeout", "hold", "tolerance"):
        if key in options and not (_number(options[key]) and options[key] >= 0):
            fail(f"{what}: `{key}` is a number")


class StepFailed(Exception):
    pass


def play(step: Step, device, out=print) -> None:
    """Do one step on its device; raise StepFailed when it does not pass."""
    timeout = float(step.options.get("timeout", DEFAULT_TIMEOUT))
    try:
        if step.action == "expect":
            device.serial(step.options.get("port", "uart")).expect(step.value, timeout)
        elif step.action == "send":
            device.serial(step.options.get("port", "uart")).send(step.value)
        elif step.action == "tap":
            device.tap(*step.value, **({"hold": step.options["hold"]}
                                       if "hold" in step.options else {}))
        elif step.action == "key":
            device.key(step.value, **({"hold": step.options["hold"]}
                                      if "hold" in step.options else {}))
        elif step.action == "type":
            device.type(step.value)
        elif step.action == "wait":
            time.sleep(float(step.value))
        elif step.action == "screen":
            wanted = Shot(Path(step.value).read_bytes())
            tolerance = int(step.options.get("tolerance", 0))
            device.screen.wait(lambda shot: shot.same_as(wanted, tolerance), timeout)
        elif step.action == "pixel":
            x, y = step.value
            rgb = tuple(step.options["rgb"])
            device.screen.wait(lambda shot: shot.pixel(x, y)[:3] == rgb, timeout)
        elif step.action == "save_screen":
            shot = device.screen.wait(lambda shot: True, timeout)
            Path(step.value).parent.mkdir(parents=True, exist_ok=True)
            Path(step.value).write_bytes(shot.png)
    except LabError as exc:
        raise StepFailed(str(exc)) from None


def run(scenario: Scenario, lab, logs: Path | None = None, out=print) -> bool:
    """Make and start the devices, play the steps in order, and stop at the
    first that fails. Every device's logs go to `logs` at the end."""
    made = {}
    try:
        for spec in scenario.devices:
            build = Build.from_dir(spec.build)
            made[spec.name] = lab.device(spec.name, build, board=spec.board,
                                         settings=spec.settings)
        for device in made.values():
            device.start(air=AIR if len(made) > 1 else None)
        out(f"started {', '.join(made)}")
        for step in scenario.steps:
            began = time.monotonic()
            try:
                play(step, made[step.device], out)
            except StepFailed as exc:
                out(f"FAIL step {step.number}, {step.describe()}: {exc}")
                return False
            out(f"ok   step {step.number}, {step.describe()} "
                f"({time.monotonic() - began:.1f} s)")
        return True
    finally:
        for device in made.values():
            device.stop()
            if logs is not None:
                logs.mkdir(parents=True, exist_ok=True)
                try:
                    device.save_logs(logs / f"{device.name}.zip")
                except LabError:
                    pass


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="boardemul scenario",
                                 description="Run a scenario file (ADR 0185)")
    ap.add_argument("file", help="the scenario, a TOML file")
    ap.add_argument("--logs", default="boardemul-logs",
                    help="where each device's logs are kept")
    args = ap.parse_args(argv)
    try:
        scenario = load(args.file)
        for spec in scenario.devices:
            Build.from_dir(spec.build)
    except (ScenarioError, BuildError) as exc:
        print(f"boardemul scenario: {exc}", file=sys.stderr)
        return 2
    from boardemul.lab import Lab
    try:
        with Lab() as lab:
            passed = run(scenario, lab, Path(args.logs))
    except LabError as exc:
        print(f"boardemul scenario: {exc}", file=sys.stderr)
        return 2
    print("passed" if passed else "failed")
    return 0 if passed else 1
