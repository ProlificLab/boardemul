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
| [0007](0007-psram-line-mode.md) | Present PSRAM in quad line mode, though the board's part is octal | Accepted (scope amended by the second board; the mode read off the image by 0144) |
| [0008](0008-claim-unmodelled-windows.md) | Claim unmodelled peripheral windows even with no model behind them | Accepted (converse added 0015) |
| [0009](0009-desktop-viewer.md) | A desktop viewer that watches a file and draws its own device | Accepted |
| [0010](0010-input-path.md) | Deliver input by modelling GPIO interrupts and pushing them out of band | Accepted |
| [0011](0011-transaction-granularity-boundary.md) | Carry transactions, not register accesses; scope the C tier to SoC silicon | Accepted |
| [0012](0012-testing.md) | Two tiers of test: device models in milliseconds, pixels in minutes | Accepted (one slow test boots a probe of ours, 0123) |
| [0013](0013-coprocessor-enable-at-reset.md) | Enable the FPU at reset, per board, and say so out loud | Superseded by 0143 |
| [0014](0014-what-counts-as-a-frame.md) | Decide what a frame is, for a display that does not have any | Accepted |
| [0015](0015-i2s-left-unmodelled.md) | Leave I2S unmodelled, and record why the obvious route fails | Superseded by 0016 |
| [0016](0016-i2s-receive-path.md) | Model the I2S receive path, from the reference manual | Accepted |
| [0017](0017-carry-one-qemu-patch.md) | Carry one patch to QEMU's own logic, and send it upstream | Accepted (upstream since 0138) |
| [0018](0018-radio-left-unmodelled.md) | Leave the radio unmodelled, and record how far the trail goes | Superseded by 0019 |
| [0019](0019-radio-bring-up-gates.md) | Answer the radio's four bring-up waits, each read off its own loop | Accepted |
| [0020](0020-interactive-vendor-firmware.md) | The vendor interface takes input; the last two walls were an ADC and a pin level | Accepted |
| [0021](0021-vendor-filesystem-image.md) | Assemble the flash with the vendor's filesystem image where the vendor's installer puts it | Accepted |
| [0022](0022-spi1-dummy-cycles-by-line-count.md) | Convert SPI1 dummy cycles with the read mode's line count | Accepted (upstream since 0138) |
| [0023](0023-bluetooth-clock-registers.md) | Keep the two Bluetooth clock registers the controller checks | Accepted (upstream since 0138) |
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
| [0057](0057-a-network-the-firmware-can-see.md) | A network the firmware can see | Accepted (one cost corrected by 0060; decision 2 superseded by 0110) |
| [0058](0058-the-firmware-tries-to-connect.md) | The firmware tries to connect, and where it stops | Accepted (central claim corrected by 0059) |
| [0059](0059-the-transmit-engine.md) | The transmit engine | Accepted |
| [0060](0060-an-access-point-that-answers.md) | An access point that answers | Accepted (its account of the failure corrected by 0061) |
| [0061](0061-the-station-authenticates.md) | The station authenticates | Accepted (its suspect cleared by 0062) |
| [0062](0062-the-transmit-result-register.md) | The transmit result register | Accepted (open question closed by 0063) |
| [0063](0063-the-station-associates.md) | The station associates | Accepted |
| [0064](0064-the-access-point-moves-to-python.md) | The access point moves to Python, behind a frame channel | Accepted (its blocker removed by 0066) |
| [0065](0065-aes-by-hand.md) | AES by hand, and what can be proved about it | Accepted |
| [0066](0066-the-cross-thread-tlb-flush.md) | The cross-thread TLB flush | Accepted (fixes the defect 0041 works around; upstream since 0138) |
| [0067](0067-a-frame-the-peer-starts.md) | A frame the peer starts, and a radio that loses it | Accepted |
| [0068](0068-a-frame-the-station-is-waiting-for.md) | A frame the station is waiting for | Accepted |
| [0069](0069-the-transmit-frame-is-dma-descriptor.md) | The transmitted frame is where the DMA descriptor points | Accepted |
| [0070](0070-the-eapol-frame-ends-where-it-says.md) | The EAPOL frame ends where it says it does | Accepted |
| [0071](0071-message-four-leaves-by-a-path-the-device-cannot-see.md) | Message four leaves by a path the device cannot see | Accepted (its explanation retracted by 0073) |
| [0072](0072-message-four-is-abandoned-at-the-idle-check.md) | Message four is abandoned at the idle check, not the cipher | Superseded by 0073 |
| [0073](0073-message-four-is-never-submitted.md) | Message four is never submitted, and the transmit path is sound | Accepted (central measurement amended by 0074) |
| [0074](0074-the-four-way-handshake-completes.md) | The four-way handshake completes, and the station leaves anyway | Milestone retracted by 0078 (the completion was a miscount) |
| [0075](0075-the-register-trace-opens-on-a-chosen-frame.md) | The register trace opens on a chosen frame | Accepted (instrument; the wall it served was misdiagnosed, see 0078) |
| [0076](0076-message-one-waits-for-the-station.md) | Message one waits for the station to be ready | Hypothesis refuted by its own test; superseded context by 0078 |
| [0077](0077-there-are-no-hardware-key-slots-to-model.md) | There are no hardware key slots to model; the crypto is software | Accepted (corrects 0076's addendum) |
| [0078](0078-the-handshake-never-completed.md) | The handshake never completed; the peer miscounted a retransmit | Accepted (retracts 0074) |
| [0079](0079-message-three-is-dropped-in-the-wrong-state.md) | Message three is dropped because the supplicant is in the wrong state | Its rejection relocated by 0080 (MIC, not state) |
| [0080](0080-message-three-fails-mic-verification-in-the-firmware.md) | Message three fails MIC verification in the firmware | Accepted (its RX-delivery suspicion disproved by 0081) |
| [0081](0081-message-three-mic-dump.md) | The message-three MIC buffers, dumped: a clean frame, a wrong MIC | Accepted |
| [0082](0082-the-firmware-miscomputes-the-mic.md) | The firmware miscomputes the MIC: a SHA-accelerator emulation defect | Accepted |
| [0083](0083-the-gdma-could-not-reach-psram.md) | The GDMA could not reach PSRAM, so DMA SHA over a received frame failed | Accepted (fixes the defect 0082 located; upstream since 0138) |
| [0084](0084-a-station-that-gets-an-ip.md) | A station that gets an IP: identity CCMP, ARP and DHCP in the peer | Accepted |
| [0085](0085-the-station-resolves-names-and-gets-the-time.md) | The station resolves names and gets the time: DNS and NTP in the peer | Accepted |
| [0086](0086-answer-mdns-to-see-what-comes-next.md) | Answer mDNS, to see what the app reaches for next | Accepted |
| [0087](0087-accept-a-tcp-connection-and-read-its-opening.md) | Accept a TCP connection and read its opening, nothing more | Accepted |
| [0088](0088-a-faithful-lan-not-a-captive-one.md) | The peer becomes a faithful LAN, not a captive one | Accepted (direction) |
| [0089](0089-a-real-tcp-transport.md) | A real TCP transport, driven a segment at a time | Accepted |
| [0090](0090-an-authoritative-resolver.md) | An authoritative resolver: a zone, and NXDOMAIN outside it | Accepted |
| [0091](0091-a-dhcp-pool-and-replies-to-the-source.md) | A DHCP pool, and replies to the source | Accepted |
| [0092](0092-real-ccmp-the-cipher-is-offloaded-to-the-mac.md) | Real CCMP: the cipher is offloaded to the MAC, not the accelerator | Accepted (plan; corrects 0084's premise) |
| [0093](0093-the-peers-ccmp-framing.md) | The peer's CCMP framing | Accepted |
| [0094](0094-real-ccmp-on-the-air.md) | Real CCMP on the air: the MAC encrypts, keyed by the peer | Accepted |
| [0095](0095-a-delivered-message-one-is-not-retried-at-once.md) | A delivered message one is not retried at once | Accepted (fixes 0094's flaky handshake) |
| [0096](0096-the-factory-test-service.md) | The factory-test service on lilygo.local:8896 | Accepted |
| [0097](0097-the-uplink-a-nat-to-the-real-internet.md) | The uplink: a NAT from the LAN to the real internet | Accepted |
| [0098](0098-each-board-has-its-own-randomness.md) | Each board has its own randomness | Accepted (amends 0004's seed) |
| [0099](0099-a-board-that-panics-may-reboot.md) | A board that panics may reboot, as silicon does | Accepted (decision 3 extended by 0114) |
| [0100](0100-the-usb-serial-port.md) | The USB serial port, with a host on the other end | Accepted |
| [0101](0101-the-interrupt-matrix-remembers-its-inputs.md) | The interrupt matrix remembers its inputs | Accepted (carried patch; upstream since 0138) |
| [0102](0102-the-max-radio-is-on-select-three.md) | The Max's radio is on chip select 3 | Accepted (corrects 0043; decision 2 superseded by 0111) |
| [0103](0103-the-max-fuel-gauge-is-provisioned.md) | The Max's fuel gauge is provisioned, as its driver expects | Accepted |
| [0104](0104-the-parts-can-read-the-guests-clock.md) | The board's parts can read the guest's clock | Accepted |
| [0105](0105-the-max-serial-parts.md) | The Max's serial parts: a modem and a u-blox receiver | Accepted (carried patch; upstream since 0138) |
| [0106](0106-what-the-census-found-harmless.md) | What the hardware census found harmless, and what it left open | Accepted |
| [0107](0107-a-skipped-event-is-decided-on-the-kick.md) | A skipped Bluetooth event is decided on the kick | Accepted (refines 0025) |
| [0108](0108-the-sensor-hub-boots-its-firmware.md) | The Max's sensor hub boots the firmware it is given | Accepted (closes 0106 item 5) |
| [0109](0109-the-air-acknowledges-retries-and-holds-for-a-dozing-station.md) | The air acknowledges, retries, and holds frames for a dozing station | Accepted (closes 0106 item 6) |
| [0110](0110-the-access-point-has-one-channel.md) | The access point has one channel | Accepted (supersedes 0057 decision 2) |
| [0111](0111-the-radio-says-when-it-is-busy.md) | The radio says when it is busy | Accepted (closes 0106 item 7; supersedes 0102 decision 2) |
| [0112](0112-the-radio-keeps-the-guests-time.md) | The radio keeps the guest's time | Accepted (moves 0037's timing onto the guest clock) |
| [0113](0113-boards-on-one-air-keep-one-time.md) | Boards on one air keep one time | Accepted (finishes 0112; closes 0111's last cost) |
| [0114](0114-the-boot-test-forgives-one-panic-by-name.md) | The boot test forgives one panic, by name | Accepted (extends 0099 decision 3; decision 3's banner-only exception withdrawn by 0123) |
| [0115](0115-the-guest-clock-does-not-follow-the-host.md) | The guest's clock does not follow the host's | Accepted (answers 0113 decision 7) |
| [0116](0116-the-l76k-keeps-the-boards-time.md) | The L76K keeps the board's time, and talks from power-on | Accepted (withdraws 0052's silence; narrows its two-sentence cost) |
| [0117](0117-the-u-blox-writes-what-its-description-writes.md) | The u-blox writes what its interface description writes | Accepted (refines 0105 decision 2; answers a 0116 cost) |
| [0118](0118-the-card-streams-a-multiple-block-transfer.md) | The card streams a multiple-block transfer | Accepted (extends 0033) |
| [0119](0119-audio-runs-at-its-sample-rate.md) | Audio runs at its sample rate | Accepted (refines 0016 decision 2) |
| [0120](0120-an-spi-transfer-takes-its-bus-time.md) | An SPI transfer takes its bus time | Accepted (refines 0032) |
| [0121](0121-an-i2c-transaction-takes-its-bus-time.md) | An I2C transaction takes its bus time | Accepted (refines 0011) |
| [0122](0122-a-serial-line-keeps-no-backlog.md) | A serial line keeps no backlog | Accepted (carried patch; found by 0120/0121; upstream since 0138) |
| [0123](0123-one-core-resets-the-other-and-holds-it.md) | One core resets the other, and holds it | Accepted (withdraws 0114's banner-only exception; decision 4 amended; upstream since 0138) |
| [0124](0124-the-max-test-reboots-from-the-same-panic.md) | The Max's test reboots from the same panic | Accepted (extends 0114 to test_max_parts) |
| [0125](0125-every-single-boot-test-reboots-from-the-same-panic.md) | Every single-board boot test reboots from the same panic | Accepted (extends 0114 and 0124) |
| [0126](0126-the-t-deck-skin-is-drawn-from-lilygos-own-sources.md) | The T-Deck skin is drawn from LilyGO's own sources | Accepted |
| [0127](0127-the-t-deck-keyboard-behaves-as-lilygos-sketch-does.md) | The T-Deck keyboard behaves as LilyGO's sketch does | Accepted (extends 0126) |
| [0128](0128-the-t-deck-trackball-rolls-and-clicks.md) | The T-Deck's trackball rolls and clicks | Accepted (extends 0026) |
| [0129](0129-the-window-shows-the-t-deck-keyboards-backlight.md) | The window shows the T-Deck keyboard's backlight | Accepted (extends 0126, 0127) |
| [0130](0130-the-max-face-is-drawn-from-lilygos-case.md) | The Max's face is drawn from LilyGO's own case | Accepted (closes 0126's Max item) |
| [0131](0131-the-maxs-three-touch-keys-report-as-key-events.md) | The Max's three touch keys report as key events | Accepted (closes 0130's open item) |
| [0132](0132-the-t-decks-keyboard-interrupt-line-is-never-driven.md) | The T-Deck's keyboard interrupt line is never driven | Accepted (closes 0127's open item) |
| [0133](0133-a-device-folder-keeps-a-device-between-runs.md) | A device folder keeps a device between runs | Accepted |
| [0134](0134-the-window-starts-a-device.md) | The window starts a device | Accepted (extends 0133) |
| [0135](0135-a-browser-is-the-window.md) | A browser is the window | Accepted (amended by 0139, 0140) |
| [0136](0136-a-teardown-that-hangs-leaves-its-own-autopsy.md) | A teardown that hangs leaves its own autopsy | Accepted |
| [0137](0137-the-max-board-carries-the-t-decks-wifi.md) | The Max board carries the T-Deck's WiFi | Accepted |
| [0138](0138-qemu-moves-to-the-tag-that-carries-nine-of-our-fixes.md) | QEMU moves to the tag that carries nine of our fixes | Accepted (ends nine carried patches) |
| [0139](0139-the-web-server-answers-only-its-own-page.md) | The web server answers only its own page | Accepted (amends 0135) |
| [0140](0140-a-run-starts-in-the-build-container-where-it-cannot-start-natively.md) | A run starts in the build container where it cannot start natively | Accepted (amends 0133, 0134, 0135) |
| [0141](0141-the-radio-says-a-packet-is-arriving-and-a-sweep-leaves-rx.md) | The radio says a packet is arriving, and a sweep leaves RX | Accepted (closes 0111's CAD exception; amends 0113) |
| [0142](0142-our-devices-re-drive-their-interrupt-after-a-reset.md) | Our devices re-drive their interrupt after a reset | Accepted (follows up 0138) |
| [0143](0143-the-fpu-is-on-out-of-reset-as-on-silicon.md) | The FPU is on out of reset, as on silicon, and our device goes | Accepted (supersedes 0013; follows up 0138) |
| [0144](0144-the-image-says-its-psram-line-mode.md) | The image says which PSRAM line mode it was built for | Accepted (amends 0007) |
| [0145](0145-a-device-keeps-its-settings-and-meets-others-on-a-named-air.md) | A device keeps its run settings, and meets others on a named air | Accepted (amends 0036, 0045, 0133, 0139) |
| [0146](0146-sound-comes-out-and-goes-in.md) | Sound comes out of the speaker, and goes into the microphone | Accepted (ends 0016's and 0119's silence) |
| [0147](0147-a-new-machine-is-ready-in-three-commands.md) | A new machine is ready in three commands, and says what it lacks | Accepted (amends 0003's manifest) |
| [0148](0148-the-max-carries-the-chips-sound-and-bluetooth.md) | The Max board carries the chip's I2S controllers and Bluetooth core | Accepted (follows 0137) |
| [0149](0149-a-device-keeps-snapshots-and-its-card-is-yours.md) | A device keeps snapshots of its storage, and its card is yours | Accepted (adds to 0133) |
| [0150](0150-a-firmware-brings-its-filesystem.md) | A firmware brings its filesystem, as its installer would | Accepted (adds to 0021, 0147) |
| [0151](0151-the-screen-shows-its-backlight.md) | The T-Deck's screen shows its backlight | Accepted (takes up 0053's undimmed frames) |
| [0152](0152-the-max-carries-the-chips-sar-and-its-scan-finishes.md) | The Max carries the chip's SAR block, and its WiFi scan finishes | Accepted (corrects 0137's evidence) |
| [0153](0153-the-page-says-what-the-machine-lacks.md) | The page says what the machine lacks | Accepted (carries 0147's doctor into the page) |
| [0154](0154-our-devices-reset-with-the-digital-core.md) | Our devices reset with the digital core, and keep their state across a CPU reset | Accepted (takes up 0142's last cost) |
| [0155](0155-a-device-can-be-cloned-with-a-mac-of-its-own.md) | A device can be cloned, with a MAC of its own | Accepted (adds to 0133, 0149) |
| [0156](0156-a-person-types-into-the-serial-ports.md) | The page reads both serial ports, and a person can type into them | Accepted (adds to 0100, 0135) |
| [0157](0157-bluetooth-stops-at-a-link-that-stays-up.md) | Bluetooth stops at a link that stays up, and this is the road past it | Accepted (after 0044–0051) |
| [0158](0158-a-serial-port-is-reachable-over-tcp.md) | A device's serial port is reachable over TCP, for the host's tools | Accepted (adds to 0156) |
| [0159](0159-one-command-starts-boardemul.md) | One command starts BoardEmul, on a runtime image with QEMU inside | Accepted (amends 0002 decision 2, adds to 0147) |
| [0160](0160-a-device-is-made-from-the-firmware-library.md) | A device is made from the firmware library | Accepted (adds to 0135, 0147) |
| [0161](0161-the-cores-take-turns-every-millisecond.md) | The cores take turns every millisecond | Accepted (adds to 0041) |
| [0162](0162-a-version-a-changelog-and-a-check-on-every-push.md) | BoardEmul has a version, a changelog and a check on every push | Accepted |
| [0163](0163-a-runs-logs-download-as-one-file.md) | A run's logs download as one file, saying what they ran on | Accepted (adds to 0135, 0162) |
| [0164](0164-a-host-port-reaches-the-device-over-wifi.md) | A port of the host reaches a TCP port of the device over WiFi | Accepted (takes ADR 0158's second road; adds to 0089, 0097) |
| [0165](0165-light-sleep-wakes-on-its-timer-and-its-pins.md) | Light sleep wakes on its timer and its pins | Accepted (adds to 0151) |
| [0166](0166-the-usb-otg-port-has-a-host.md) | The USB OTG port has a host: a firmware's TinyUSB serial port is read | Accepted (takes ADR 0158's first road) |
| [0167](0167-tests-whose-deadlines-follow-the-host-run-alone.md) | Slow tests whose firmware keeps deadlines on the host's time run alone | Accepted |
| [0168](0168-the-bluetooth-link-transmits-what-the-controller-stages.md) | The Bluetooth link transmits what the controller stages | Accepted (step 1 of 0157) |
| [0169](0169-a-central-reads-meshtastic-over-bluetooth.md) | A central reads Meshtastic over Bluetooth | Accepted (steps 3 and 5 of 0157) |
| [0170](0170-a-register-access-does-not-end-a-block-without-icount.md) | A register access does not end a translation block without `-icount` | Accepted (adds a carried patch, 0017) |
| [0171](0171-bluetooth-pairs-with-a-passkey-and-encrypts.md) | Bluetooth pairs with a passkey and encrypts the link | Accepted (step 4 of 0157; amends 0046) |
| [0172](0172-a-parts-answers-to-idle-bytes-come-ahead.md) | A part's answers to idle bytes come ahead of the bytes | Accepted (extends 0011) |
| [0173](0173-a-bluetooth-frame-carries-its-access-address.md) | A Bluetooth frame carries its access address | Accepted (part of 0157 step 2; amends 0046, 0168, 0171) |
| [0174](0174-what-qemu-writes-is-kept-local-and-copied.md) | What QEMU writes during a run is kept on local disk and copied out | Accepted (revises 0164's account of the WiFi join) |
| [0175](0175-what-python-writes-is-kept-off-the-guests-path.md) | What the run's Python side writes is kept off the guest's path | Accepted (follows 0174 to the panel's frames and the OTG port's log) |
| [0176](0176-qemu-is-profiled-by-sampling-and-trace-windows-are-read-from-a-shadow.md) | QEMU is profiled by sampling, and trace windows are read from a shadow | Accepted (profiles ADR 0161's speed; amends 0011's protocol) |
| [0177](0177-a-received-wifi-frame-has-a-signal-strength.md) | A received WiFi frame has a signal strength | Accepted (amends 0152; adds to 0063's receive header) |
