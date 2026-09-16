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
| [0007](0007-psram-line-mode.md) | Present PSRAM in quad line mode, though the board's part is octal | Accepted (scope amended by the second board) |
| [0008](0008-claim-unmodelled-windows.md) | Claim unmodelled peripheral windows even with no model behind them | Accepted (converse added 0015) |
| [0009](0009-desktop-viewer.md) | A desktop viewer that watches a file and draws its own device | Accepted |
| [0010](0010-input-path.md) | Deliver input by modelling GPIO interrupts and pushing them out of band | Accepted |
| [0011](0011-transaction-granularity-boundary.md) | Carry transactions, not register accesses; scope the C tier to SoC silicon | Accepted |
| [0012](0012-testing.md) | Two tiers of test: device models in milliseconds, pixels in minutes | Accepted |
| [0013](0013-coprocessor-enable-at-reset.md) | Enable the FPU at reset, per board, and say so out loud | Accepted |
| [0014](0014-what-counts-as-a-frame.md) | Decide what a frame is, for a display that does not have any | Accepted |
| [0015](0015-i2s-left-unmodelled.md) | Leave I2S unmodelled, and record why the obvious route fails | Superseded by 0016 |
| [0016](0016-i2s-receive-path.md) | Model the I2S receive path, from the reference manual | Accepted |
| [0017](0017-carry-one-qemu-patch.md) | Carry one patch to QEMU's own logic, and send it upstream | Accepted |
| [0018](0018-radio-left-unmodelled.md) | Leave the radio unmodelled, and record how far the trail goes | Superseded by 0019 |
| [0019](0019-radio-bring-up-gates.md) | Answer the radio's four bring-up waits, each read off its own loop | Accepted |
| [0020](0020-interactive-vendor-firmware.md) | The vendor interface takes input; the last two walls were an ADC and a pin level | Accepted |
| [0021](0021-vendor-filesystem-image.md) | Assemble the flash with the vendor's filesystem image where the vendor's installer puts it | Accepted |
| [0022](0022-spi1-dummy-cycles-by-line-count.md) | Convert SPI1 dummy cycles with the read mode's line count | Accepted |
| [0023](0023-bluetooth-clock-registers.md) | Keep the two Bluetooth clock registers the controller checks | Accepted |
| [0024](0024-bluetooth-bring-up.md) | Bluetooth bring-up: what the controller checked, and what answered it | Accepted |
| [0025](0025-ble-core-timers-in-c.md) | The link-layer core's clock and timers run in C, on QEMU's clock | Accepted |
| [0026](0026-trackball-at-rest.md) | The trackball rests released | Accepted |
| [0027](0027-instruction-counted-clock.md) | An instruction-counted clock, as an option | Accepted |
| [0028](0028-a-quiet-screen-is-a-frame.md) | A quiet screen is a frame | Accepted |
| [0029](0029-touch-interrupt-follows-the-mode-written.md) | The touch interrupt line follows the mode the firmware wrote | Accepted |
| [0030](0030-the-touch-controller-reports-in-its-own-raster.md) | The touch controller reports in its own raster | Accepted |
| [0031](0031-the-touch-interrupt-asserts-on-a-report.md) | The touch interrupt asserts on a report and idles between | Accepted |
| [0032](0032-more-than-one-device-on-the-spi-bus.md) | More than one device on the SPI bus | Accepted |
| [0033](0033-a-card-in-the-slot.md) | A card in the slot | Accepted |
| [0034](0034-a-radio-that-answers.md) | A radio that answers | Accepted |
| [0035](0035-a-reset-reaches-the-models-outside.md) | A reset reaches the models outside | Accepted |
| [0036](0036-the-air-between-two-boards.md) | The air between two boards | Accepted |
| [0037](0037-what-the-air-does-to-a-packet.md) | What the air does to a packet | Accepted |
| [0038](0038-how-far-a-packet-carries.md) | How far a packet carries | Accepted |
| [0039](0039-the-place-between-the-boards.md) | The place between the boards | Accepted |
| [0040](0040-stop-a-run-when-the-evidence-is-in.md) | Stop a run when the evidence is in | Accepted |
| [0041](0041-one-translation-thread.md) | One translation thread | Accepted |
| [0042](0042-somewhere-to-put-an-attempt.md) | Somewhere to put an attempt | Accepted |
| [0043](0043-the-other-board-gets-the-same-parts.md) | The other board gets the same parts | Accepted |
| [0044](0044-what-bluetooth-would-need-to-be-on-the-air.md) | What Bluetooth would need to be on the air | Accepted |
| [0045](0045-bluetooth-gets-an-air-of-its-own.md) | Bluetooth gets an air of its own | Accepted |
| [0046](0046-the-controller-can-be-spoken-to.md) | The controller can be spoken to | Accepted |
| [0047](0047-a-central-connects.md) | A central connects | Corrected by 0048 |
| [0048](0048-the-connection-is-made-and-unmade-at-once.md) | The connection is made and unmade at once | Accepted |
| [0049](0049-a-received-packet-says-when-it-arrived.md) | A received packet says when it arrived | Accepted |
| [0050](0050-what-a-connection-event-is-told.md) | What a connection event is told | Superseded by 0051 |
| [0051](0051-a-link-that-stays-up.md) | A link that stays up | Accepted |
| [0052](0052-a-part-on-a-serial-line.md) | A part on a serial line | Accepted |
| [0053](0053-the-screen-has-a-brightness.md) | The screen has a brightness | Accepted |
| [0054](0054-what-wifi-would-need-to-hear-something.md) | What WiFi would need to hear something | Mostly answered by 0055 |
| [0055](0055-a-frame-the-wifi-driver-accepts.md) | A frame the WiFi driver accepts | Accepted |
| [0056](0056-what-tells-the-driver-a-frame-arrived.md) | What tells the driver a frame arrived | Accepted (supersedes half of 0055) |
| [0057](0057-a-network-the-firmware-can-see.md) | A network the firmware can see | Accepted (one cost corrected by 0060) |
| [0058](0058-the-firmware-tries-to-connect.md) | The firmware tries to connect, and where it stops | Accepted (central claim corrected by 0059) |
| [0059](0059-the-transmit-engine.md) | The transmit engine | Accepted |
| [0060](0060-an-access-point-that-answers.md) | An access point that answers | Accepted (its account of the failure corrected by 0061) |
| [0061](0061-the-station-authenticates.md) | The station authenticates | Accepted (its suspect cleared by 0062) |
| [0062](0062-the-transmit-result-register.md) | The transmit result register | Accepted (open question closed by 0063) |
| [0063](0063-the-station-associates.md) | The station associates | Accepted |
| [0064](0064-the-access-point-moves-to-python.md) | The access point moves to Python, behind a frame channel | Accepted (its blocker removed by 0066) |
| [0065](0065-aes-by-hand.md) | AES by hand, and what can be proved about it | Accepted |
| [0066](0066-the-cross-thread-tlb-flush.md) | The cross-thread TLB flush | Accepted (fixes the defect 0041 works around) |
