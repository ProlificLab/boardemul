# Architecture Decision Records

One file per decision. Context / Decision / Consequences / Alternatives
rejected. Numbered in the order taken, never renumbered, never deleted — a
superseded ADR is marked superseded and points forward.

| # | Title | Status |
|---|---|---|
| [0001](0001-qemu-fork-as-cpu-engine.md) | Use the Espressif QEMU fork as the CPU and SoC engine | Accepted |
| [0002](0002-devcontainer-and-qemu-build.md) | Build QEMU from source in a devcontainer | Accepted |
| [0003](0003-flash-image-assembly.md) | Assemble the flash image and eFuse blob by parsing, not by convention | Accepted |
| [0004](0004-running-and-observing.md) | Launch QEMU deterministically, and observe with the memory-region trace | Accepted |
| [0005](0005-bus-bridge.md) | Forward unmodelled MMIO out of QEMU over a Unix socket | Accepted |
