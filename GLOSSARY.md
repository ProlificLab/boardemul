# BoardEmul

An emulator that runs real, unmodified firmware for ESP32-S3 boards, so the
people who write, teach or ship that firmware can run and test it without
the hardware. It comes with two boards, the LilyGO T-Deck and T-Deck Max,
and lets its users describe their own.

## Language

### What runs

**Build**:
The files a firmware project produces for one board: its application image
and, when it has one, the image of its filesystem. What a developer brings.
_Avoid_: binary, image, bin

**Library firmware**:
A firmware published by its vendor and pinned by BoardEmul's manifest,
fetched from where the vendor publishes it and never redistributed.
_Avoid_: stock firmware, built-in firmware

**Device**:
One emulated unit: a board, the build it runs, its own storage and its
settings, kept from one run to the next.
_Avoid_: node, instance, emulator

### What it runs on

**Board**:
The hardware a device reproduces: its chip and the parts wired to that
chip's pins.
_Avoid_: target, platform, machine

**Board description**:
What a user writes to define a board: which chip, and which parts are wired
to which pins.
_Avoid_: board profile, board file, board config

**Part**:
A component wired on a board, outside the chip: a radio, a screen, a card,
a keyboard, a sensor.
_Avoid_: device, peripheral, component

**Peripheral**:
A block inside the chip: an SPI controller, the WiFi MAC, a timer.
_Avoid_: part, device
