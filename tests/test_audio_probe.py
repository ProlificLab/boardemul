#!/usr/bin/env python3
"""Sound goes into the microphone and comes out of the speaker (ADR 0146).

The probe records a second from I2S1 through ESP-IDF 4.4's I2S driver and
says the strongest frequency it heard, then plays a 1 kHz tone on I2S0. The
run's microphone is a 440 Hz tone, and what the speaker played is a WAV file
in the run's directory.

    python tools/audio_probe/build.py
    python3 -m pytest tests/test_audio_probe.py -m slow
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    FIRMWARE_AUDIO_PROBE, Run, qemu_available, run_emulator,
)
from tools.tone import measure, write_tone  # noqa: E402

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FIRMWARE_AUDIO_PROBE.exists(),
                       reason="probe not built; run tools/audio_probe/build.py"),
    pytest.mark.skipif(not qemu_available(), reason="QEMU not built"),
]

HEARD = re.compile(r"probe: heard (\d+) frames, rms (\d+), strongest (\d+) Hz")


def _done(uart: str, frames: list) -> bool:
    return "probe: done" in uart


@pytest.fixture(scope="module", params=["tdeck", "tdeck-max"])
def probed(request, tmp_path_factory) -> tuple[Run, Path]:
    """On both boards: the controllers are the chip's (ADR 0148)."""
    work = tmp_path_factory.mktemp("audio")
    tone = work / "tone.wav"
    write_tone(tone, 440.0, 5.0)
    run = run_emulator(work / "run", firmware=FIRMWARE_AUDIO_PROBE, board=request.param,
                       timeout=120, extra=("--audio", "--mic", str(tone)),
                       until=_done)
    return run, work / "run"


def test_the_microphone_hears_the_file(probed) -> None:
    """A full second at the stream's rate, at the file's level, at its pitch.
    Before ADR 0146 the probe never got this far: the driver waits for
    RX_UPDATE to clear itself, which silicon does and the model did not."""
    run, _outdir = probed
    found = HEARD.search(run.uart)
    assert found, run.uart[-1500:]
    frames, rms, hz = (int(v) for v in found.groups())
    assert frames == 16000
    # A sine at half of full scale: RMS 0.5 * 32767 / sqrt(2).
    assert abs(rms - 11585) < 200
    assert hz == 440


def test_the_speaker_plays_into_a_wav_file(probed) -> None:
    run, outdir = probed
    assert "probe: played 1000 Hz for 1 s" in run.uart, run.uart[-1500:]
    streams = sorted((outdir / "audio").glob("i2s0-*.wav"))
    assert len(streams) == 1, list((outdir / "audio").iterdir())
    found = measure(streams[0])
    # The tone, at its level: amplitude 12000 is -11.7 dBFS RMS.
    assert abs(found["hz"] - 1000) < 5, found
    assert abs(found["dbfs"] - (-11.7)) < 0.5, found
    assert found["loud_seconds"] >= 1.0, found
