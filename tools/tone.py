#!/usr/bin/env python3
"""Write a test tone as a PCM WAV file, and say what tone a WAV file holds.

    python tools/tone.py write tone.wav --hz 440 --seconds 5
    python tools/tone.py measure logs/run/audio/i2s0-1.wav

The microphone of a run can be fed a file (`run.py --mic`) and what the
speaker plays is written to one (`run.py --audio`, ADR 0146); this is the
pair of ends a test holds. The measure counts zero crossings of the first
channel over the loud part, which is enough to tell a 440 Hz tone from
silence or from another tone, and says the level as RMS in dBFS.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import wave
from pathlib import Path


def write_tone(path: Path, hz: float, seconds: float, rate: int = 16000,
               channels: int = 2, level: float = 0.5) -> None:
    frames = int(rate * seconds)
    out = bytearray()
    for i in range(frames):
        v = int(level * 32767 * math.sin(2 * math.pi * hz * i / rate))
        out += struct.pack("<h", v) * channels
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(out))


def first_channel(path: Path) -> tuple[list[int], int]:
    with wave.open(str(path), "rb") as wav:
        width, channels, rate = wav.getsampwidth(), wav.getnchannels(), wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise SystemExit(f"{path}: {width * 8}-bit samples; only 16-bit is measured")
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    return list(samples[::channels]), rate


def measure(path: Path) -> dict:
    """The tone's frequency in Hz and level in dBFS over the samples louder
    than a hundredth of full scale, and how many seconds that is."""
    samples, rate = first_channel(path)
    loud = [i for i, v in enumerate(samples) if abs(v) > 327]
    if not loud:
        return {"seconds": len(samples) / rate, "loud_seconds": 0.0,
                "hz": 0.0, "dbfs": float("-inf")}
    span = samples[loud[0]:loud[-1] + 1]
    crossings = sum(1 for a, b in zip(span, span[1:]) if (a < 0) != (b < 0))
    rms = math.sqrt(sum(v * v for v in span) / len(span))
    return {"seconds": len(samples) / rate, "loud_seconds": len(span) / rate,
            "hz": crossings / 2 / (len(span) / rate),
            "dbfs": 20 * math.log10(rms / 32768) if rms else float("-inf")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write")
    w.add_argument("path", type=Path)
    w.add_argument("--hz", type=float, default=440.0)
    w.add_argument("--seconds", type=float, default=5.0)
    w.add_argument("--rate", type=int, default=16000)
    w.add_argument("--channels", type=int, default=2)
    m = sub.add_parser("measure")
    m.add_argument("path", type=Path)
    args = ap.parse_args(argv)
    if args.command == "write":
        write_tone(args.path, args.hz, args.seconds, args.rate, args.channels)
        print(f"wrote {args.path}")
    else:
        found = measure(args.path)
        print(f"{args.path}: {found['seconds']:.2f} s, loud for "
              f"{found['loud_seconds']:.2f} s, {found['hz']:.1f} Hz, "
              f"{found['dbfs']:.1f} dBFS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
