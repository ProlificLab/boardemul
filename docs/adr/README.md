# Architecture Decision Records

One file per decision. Context / Decision / Consequences / Alternatives
rejected. Numbered in the order taken, never renumbered, never deleted — a
superseded ADR is marked superseded and points forward.

| # | Title | Status |
|---|---|---|
| [0001](0001-qemu-fork-as-cpu-engine.md) | Use the Espressif QEMU fork as the CPU and SoC engine | Accepted (boundary rule amended by 0011) |
| [0002](0002-devcontainer-and-qemu-build.md) | Build QEMU from source in a devcontainer | Accepted |
| [0003](0003-flash-image-assembly.md) | Assemble the flash image and eFuse blob by parsing, not by convention | Accepted |
| [0004](0004-running-and-observing.md) | Launch QEMU deterministically, and observe with the memory-region trace | Accepted |
| [0005](0005-bus-bridge.md) | Forward unmodelled MMIO out of QEMU over a Unix socket | Accepted (wire format amended by 0011) |
| [0006](0006-peripheral-and-panel-models.md) | Model peripherals in Python, and learn their behaviour from the wire | Accepted |
| [0007](0007-psram-line-mode.md) | Present PSRAM in quad line mode, though the board's part is octal | Accepted |
| [0008](0008-claim-unmodelled-windows.md) | Claim unmodelled peripheral windows even with no model behind them | Accepted |
| [0009](0009-desktop-viewer.md) | A desktop viewer that watches a file and draws its own device | Accepted |
| [0010](0010-input-path.md) | Deliver input by modelling GPIO interrupts and pushing them out of band | Accepted |
| [0011](0011-transaction-granularity-boundary.md) | Carry transactions, not register accesses; scope the C tier to SoC silicon | Accepted |
| [0012](0012-testing.md) | Two tiers of test: device models in milliseconds, pixels in minutes | Accepted |
