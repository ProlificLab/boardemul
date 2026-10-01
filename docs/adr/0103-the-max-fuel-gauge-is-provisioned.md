# ADR 0103 — The Max's fuel gauge is provisioned, as its driver expects

**Status:** Accepted — 2026-10-01.

## Context

The BQ27220 on the T-Deck Max was a plain register file with a voltage and a
state of charge in it. Over its USB port (ADR 0100) the factory demo said what
that cost:

    (210) Invalid Device Number 0000 != 0x0220

and it stopped using the gauge.

The driver it uses is published with it: `lib/BQ27220` in
Xinyuan-LilyGO/T-Deck-MAX, with its command codes and keys in
`bq27220_def.h` and its status layouts in `bq27220.h`. It does much more than
read a device number. It checks the active profile, unseals the gauge, checks
`INITCOMP`, resets it and waits for `INITCOMP`, takes full access, enters
configuration mode and waits for `CFGUPDATE`, compares every data-memory
parameter it cares about, writes the ones that differ (each with its checksum),
leaves configuration mode, waits for `CFGUPDATE` to clear, and seals it again.
Answering only the device number moved the failure one step on, to 1,252 polls
of `OperationStatus` waiting for a bit nothing set.

## Decision

1. **The gauge speaks that protocol**, with the driver's codes:
   - subcommands as little-endian words on `Control()` (0x00) or `MACSubcmd()`
     (0x3E), answered in `MACData()` (0x40) with checksum and length after it;
   - `CONTROL_STATUS` when `Control()` itself is read: profile 0, the default;
   - `OperationStatus()` (0x3A) with the security mode, `INITCOMP` and
     `CFGUPDATE`, moved by the unseal keys, full-access key, `SEALED`, `RESET`
     and the configuration-update subcommands;
   - data memory: an address selects it for reading back; an address with data
     is written once `MACDataSum()` arrives with the right checksum and
     length, and only in configuration mode.

2. **Data memory starts empty each run.** On the part it is flash and keeps
   what was written. Every run here is therefore a gauge being provisioned for
   the first time, which the driver handles and reports
   (`(98) Data at 0x929b(2): ...`).

3. **The battery is a scenario value, stated once**: 3000 mAh design and
   full-charge capacity, 2550 mAh remaining (85 %), 100 % health, 25 °C,
   present and discharging. The factory example asks for 3000 mAh, so the
   capacity matches it.

## Consequences

**Good**

- The factory demo trusts the gauge, provisions it, and reads it.

**Costs, accepted**

- `FW_VERSION`, `HW_VERSION` and the other subcommands the driver does not use
  answer zeros. Their real values are not in any source read here.
