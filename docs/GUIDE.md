# Operating BoardEmul

This is the guide for running the emulator: getting it going, making
devices, using them, joining them into a mesh, and scripting them. Why
things are the way they are is in `docs/adr/`; you do not need it here.

## Start it

You need Python 3.11 or later and, on Windows, Docker Desktop running.
From the repository's root:

```
python boardemul.py
```

The first time, it builds the `boardemul-runtime` image, which compiles
QEMU once (ten to thirty minutes), and fetches the vendor firmware the
manifest pins (about 30 MB). Then it opens the page at
`http://127.0.0.1:8420/`. Every later start takes seconds.

`python boardemul.py --check` says what this machine lacks and how to fix
each thing, without changing anything. The page shows the same list in red
when something stops a device from starting.

## Devices

A device is a board, a firmware, and storage that is kept between runs:
its flash, its eFuses (with its MAC) and its SD card. Devices live in
`devices/`, one folder each.

**Make one.** On the page, *New device*: a name, then a firmware. Pick one
of the fetched images, and its board comes with it; or drop another `.bin`
and pick its board, T-Deck Max or T-Deck. The pinned images:

| Firmware | Board | What it is |
| --- | --- | --- |
| `T-Deck-MAX_V3.1_20260313.bin` | T-Deck Max | LilyGO's factory demo |
| `T-Deck_UnitTest_250620.bin` | T-Deck | LilyGO's unit-test build |
| `firmware-t-deck-tft-2.7.10.94d4bdf.bin` | T-Deck | Meshtastic 2.7.10; its filesystem comes with it |

**Start it** with ▶ Start. The run view shows:

- the panel, live;
- the device's face, drawn from LilyGO's own drawings. Click its keys, or
  type on your keyboard: capitals and symbols are pressed as the device's
  own chords. On the T-Deck the mouse wheel rolls the trackball, and
  pressing on the ball clicks it;
- the serial console. Switch between the UART and the USB port, and type a
  line into it: Enter sends it;
- what the speaker played, when the device's settings ask for sound;
- a scrubber through every frame of the run.

The T-Deck's screen dims and darkens as the firmware drives its backlight.

**Stop it** with ■ Stop. What the firmware saved is in the device's flash
and card for the next start.

## Settings

*Settings* on a device's card holds what every run of it is told:

- **On the air**: an air name and a place in metres. Devices with the same
  air name hear each other's LoRa radios. A Bluetooth air works the same
  way.
- **Wi-Fi**: an access point for the device to find, with a password for
  WPA2, and optionally a route to the real internet through this computer.
- **Satellites**: a GPS position, in degrees.
- **Host tools**: a TCP port that carries a serial port, for a terminal or
  a script on this computer (`127.0.0.1` only).
- **Running**: restart after a panic, as the board does; count guest time
  in instructions (Bluetooth needs it); record the speaker.

Settings apply from the next start.

## Snapshots, cards and clones

These need the device stopped.

- **Snapshot**: keeps the flash, the card and the settings under a label.
  Restore puts them all back.
- **Card**: download the SD card as a FAT16 image, or replace it with
  another image of the same size. The old card becomes a snapshot first.
  `python tools/device.py card NAME --files A.TXT B.BIN` makes a fresh card
  holding files.
- **Clone**: a copy of the device with a MAC of its own.

## A Meshtastic mesh

1. Make a device from the Meshtastic firmware and start it. Within a minute
   or two its setup dialog asks for a region and a name. Fill it promptly:
   the screen blanks after about 30 seconds without input. The device
   reboots into its home screen.
2. Stop it, and *Clone* it as many times as you want nodes. Each clone has
   its own MAC, so its own node number.
3. Give every node the same air name in *Settings*, and each its own place:
   `0,0`, `50,0`, `0,80`.
4. Start them all. They hear each other at the strength their distance
   gives: each run's bus log (`logs/devices/NAME/N/NAME.bus.log`) has a line
   `sx1262: heard a … packet at … dBm` for every packet it received.

## Scripting

Everything the page does goes through a small HTTP API on the same port,
and a run's input is a file of JSON lines.

| Request | Does |
| --- | --- |
| `GET /api/devices` | lists the devices |
| `POST /api/firmware?name=F.bin` | uploads a firmware image (the raw bytes); answers its `path` |
| `POST /api/devices` | makes one: `{"name", "board", "firmware": path}` |
| `POST /api/devices/NAME/start` | starts it; `{"args": [...]}` adds settings for this run |
| `POST /api/devices/NAME/stop` | stops it |
| `POST /api/devices/NAME/settings` | keeps settings: `{"settings": [...]}` |
| `POST /api/devices/NAME/clone` | clones it: `{"name"}` |
| `GET` / `POST /api/devices/NAME/snapshots` | lists, or takes one: `{"label"}` |
| `POST /api/devices/NAME/snapshots/LABEL/restore` | restores one |
| `GET` / `POST /api/devices/NAME/card` | downloads, or replaces, the card image |
| `GET /api/runs/NAME/frame` | the latest panel image, as PNG |
| `GET /api/runs/NAME/uart?port=uart\|usb&after=N` | a serial port's lines from line N |
| `POST /api/runs/NAME/input` | input events, below |
| `GET /api/doctor` | what this machine lacks |

Requests that change something must come from the page itself or from a
program, not from another web page. A body is JSON, except a firmware or a
card image, which is the raw bytes as `application/octet-stream`.

Input events, posted as `{"events": [...]}` or appended to a run's
`input.jsonl`:

```
{"type": "touch", "x": 120, "y": 80, "down": true}
{"type": "touch", "down": false}
{"type": "key", "key": "Q", "down": true}
{"type": "text", "text": "hello"}
{"type": "trackball", "roll": "up", "steps": 2}
{"type": "serial", "port": "usb", "text": "help\n"}
```

From a shell, `python tools/device.py` does the same for folders: `create`,
`list`, `show`, `settings`, `snapshot`, `restore`, `card`, `clone`, `start`.
`python emu/run.py --help` lists every option a run takes.

## Where things are

| | |
| --- | --- |
| a device's storage | `devices/NAME/` |
| a run's output | `logs/devices/NAME/N/`: `frames/`, `NAME.uart.log`, `NAME.usb.log`, `audio/`, `console.log` |
| why a run ended | the run's `console.log`, also shown in the page when it ends |

## Limits

Each of these is decided and recorded in an ADR, not forgotten:

- Bluetooth advertises and holds a connection, and carries no data (ADR
  0157). Phones cannot pair with a node.
- Meshtastic's own client API is not reachable: on the T-Deck it runs over
  the USB OTG controller, which is not modelled (ADR 0158).
- On the Max, the audio codec answers its identity only. The modem answers
  the factory test and has no SIM.
- GPS always has a fix at the position you give.
- Runs go at a fraction of real time, slower with many devices at once:
  Meshtastic reaches its home screen in about forty seconds, where a board
  takes a few. Both cores share one host thread, taking turns of a
  millisecond (ADR 0161). Without the instruction-counted clock, a heavily
  loaded machine can make a firmware miss its own deadlines.
