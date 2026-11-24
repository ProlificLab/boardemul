# ADR 0185 — A scenario is a TOML file of devices and steps

**Status:** Accepted — 2026-11-24. Carries out ADR 0180, decision 4
("declarative scenarios later, built on it").

## Context

The test library (ADR 0182) lets a firmware developer drive devices from
Python: start them, wait for a serial line, send one, tap, press keys,
type, and wait for the screen. Many tests are only that, in order, and
teaching and manufacturers (ADR 0180's later users) are better served by a
file they can read than by a program. Board descriptions are TOML already
(ADR 0184), read by the standard library.

## Decision

1. **A scenario is a TOML file**: the devices it runs, then its steps, in
   order.

   ```toml
   [[device]]
   name = "node"
   build = ".pio/build/t-deck"     # relative to the scenario's file
   board = "tdeck"                 # or a description, NAME.toml
   settings = ["--host-clock"]     # run settings, optional

   [[step]]
   expect = "setup done"           # a serial line matching this
   port = "usb"
   timeout = 120

   [[step]]
   tap = [120, 80]

   [[step]]
   screen = "golden/menu.png"      # wait for a frame that matches it
   timeout = 30
   ```

2. **A step does one thing**, named by its key: `expect` (a regular
   expression, on `port`, default `uart`), `send` (text to `port`), `tap`
   (`[x, y]`), `key` (a key's name), `type` (text), `wait` (seconds),
   `screen` (wait for a frame matching a PNG, within `tolerance`),
   `pixel` (wait until `[x, y]` is `rgb`), and `save_screen` (write the
   latest frame to a PNG). `device` names the device, and may be left out
   when there is one; `timeout` bounds a waiting step, 60 s by default.
   Like a board description, a scenario is read strictly: an unknown key,
   a step that does two things or none, or a device nobody declared is
   refused before anything starts.

3. **Devices on one scenario share one LoRa air**, as `boardemul test`'s do
   (ADR 0182), so several nodes hear each other.

4. **`boardemul scenario FILE` runs it** on the same page `Lab` uses, says
   each step as it passes, stops at the first that fails, keeps each
   device's logs in `--logs`, and answers 0 when every step passed, 1 when
   one failed and 2 when the file is wrong. The GitHub Action stays
   `boardemul test`; a workflow runs a scenario as its command.

## Consequences

**Good**

- A test that is a sequence needs no Python, and reads as what it checks.
- It is the library underneath, so a scenario and a Python test see the
  same devices the same way.

**Costs, accepted**

- No branching, loops or variables: a test that needs them is a Python
  test. A scenario that grows them would be a worse programming language.
