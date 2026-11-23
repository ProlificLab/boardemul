# Contributing to BoardEmul

Thank you for helping. A few rules keep this project the way it is.

## Sign your commits off

Every commit carries a Developer Certificate of Origin sign-off: you state
that you wrote the change, or otherwise have the right to submit it, under
the project's licences (https://developercertificate.org). Add it with

```
git commit -s
```

which appends `Signed-off-by: Your Name <your@address>`. The CI refuses a
pull request with an unsigned commit.

## Which licence your change is under

Everything outside `qemu-patch/` is Apache-2.0; `qemu-patch/` is
GPL-2.0-or-later, because it is compiled into QEMU (`NOTICE`,
`docs/adr/0179`). Start a new file with the same SPDX line as its
neighbours.

## How this project works

- **A decision before code.** A change of behaviour starts with an ADR in
  `docs/adr/` (see its README): the context, what was decided, and what it
  costs. Small fixes do not need one.
- **Model from the wire or a published source, never a guess.** Say in the
  ADR or the code which datasheet, trace or source a behaviour comes from.
- **The standard library only**, outside QEMU. Ask before adding a
  dependency.
- **Small commits**, one purpose each, with an imperative message.
- **Tests.** `python3 -m pytest tests/` runs the fast tier, which needs
  neither QEMU nor firmware and runs in CI. `python3 -m pytest tests/ -m
  slow` boots firmware under QEMU; run it, or `bash tools/slow_suite.sh`,
  in the build container before proposing a change to the emulator.
- **Words.** `GLOSSARY.md` says what Device, Board, Part and Build mean
  here; use them that way.

## Reporting a problem

Open an issue with the device's logs: the page's download button ("Download
this run's logs, for a report"), or
`GET /api/devices/NAME/logs.zip`, carries the run's log, its serial output
and what it ran on.
