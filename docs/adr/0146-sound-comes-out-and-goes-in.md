# ADR 0146 — Sound comes out of the speaker, and goes into the microphone

**Status:** Accepted — 2026-11-18. Ends the "silence in and nothing out" of
ADRs 0016 and 0119, on the T-Deck board.

## Context

The T-Deck's I2S controllers are ours, in C (`esp_i2s_master`, ADRs 0016,
0119). The speaker is on I2S0 and the microphone on I2S1. The firmware's
transfers complete, at the sample rate the guest set, and that is all:

- what the firmware **plays** is read out of guest memory by the DMA and
  dropped;
- what it **records** is a buffer of zeroes.

ADR 0119 measured the vendor UnitTest build: it records on I2S1, then plays
on I2S0, both as 16-bit stereo at 16,002 Hz. Today nobody can hear what it
plays, and it can only ever record silence.

Everything needed to do this faithfully is already decoded. ADR 0119 reads
the rate, the slot width and the enabled channels off the registers to pace
each stream. A sample in a buffer is what the I2S wire carries, slot after
slot, little-endian. So a played buffer is already a block of PCM, and
writing a WAV header in front of it is the whole of the format work.

## Decision

1. **What the speaker plays goes to a WAV file.** With `--audio`, each
   transmit stream, from TX_START set to TX_START clear, is written to
   `audio/<controller>-<n>.wav` in the run's directory. Its format is the
   stream's own: the rate ADR 0119 decodes, rounded to a whole hertz, its
   enabled channels, and its slot width. The header is brought up to date
   after every buffer, so the file is a valid WAV at every moment, while the
   run goes on and after it is killed.

2. **A WAV file can be the microphone.** With `--mic FILE.wav`, each receive
   stream hears the file from its start: the first capture after the stream
   starts gets the file's first samples, and after its end the microphone
   hears silence again. The file is converted to the stream's format sample
   by sample:
   - its rate to the stream's, by taking the nearest earlier sample;
   - its channels to the stream's, a mono file feeding every slot, and a
     file with fewer channels than the stream repeating them;
   - its sample width to the stream's slot width.
   PCM files of 8, 16, 24 or 32 bits are read, plain or WAVE_FORMAT_EXTENSIBLE.

3. **Off by default, and only where the controller is modelled.** Recording
   writes about 64 kB for every second of 16 kHz stereo played. A stream
   left running would fill a disk over a long run, so recording is asked
   for. The Max's I2S is a claimed window, not a controller (ADR 0008), so on
   that board both options say they are ignored, as `--ble-air` does.

4. **A slave-mode or PDM stream is not written**, because its rate is not
   known (ADR 0119, decision 3). The log says so once per stream.

5. **The page plays what was recorded.** `--audio` is a run setting a page
   may pass, and a device may keep (ADR 0145): it names no file. The run
   view lists the run's WAV files and plays them. `--mic` names a file, so
   it stays on the command line (ADR 0139).

6. **Two defects of the controller model, found on the way, are fixed.** A
   probe (`tools/audio_probe`, ESP-IDF 4.4's I2S driver, the vendor builds'
   version) records a second from I2S1, says what it heard, and plays 1 kHz
   on I2S0. It met two things silicon does that the model did not:
   - **RX_UPDATE and TX_UPDATE clear themselves.** Bit 8 of each CONF
     register is self-clearing in the TRM: hardware clears it once the
     configuration has crossed into the I2S clock domain.
     `i2s_hal_start_rx` sets it and spins until it reads 0. The model stored
     it, and the probe spun there forever (`pc=0x42025324`, in
     `i2s_hal_start_rx`). The vendor UnitTest never waited on it, which is
     why ADR 0016 did not meet it.
   - **A receive buffer's owner bit counts only when the channel checks
     it.** GDMA_IN_CONF1's CHECK_OWNER says whether the engine heeds a
     descriptor's owner bit, and ESP-IDF 4.4's driver leaves it clear. The
     model refused every buffer whose owner bit was 0, so the capture stopped
     after the first lap of the ring.

## Measured

On 2026-11-18, the probe with a 440 Hz sine at half of full scale as the
microphone (`tools/tone.py`), 16-bit stereo at 16 kHz:

- it heard 16,000 frames at RMS 11,582, against 11,585 for that sine, and
  its strongest frequency was 440 Hz;
- the speaker's file, `i2s0-1.wav`, holds 999.8 Hz at −11.7 dBFS, the 1 kHz
  tone at the amplitude it was written with (12,000).

The vendor UnitTest build 250620 with `--audio` writes two streams: a long
one that is silent but for a quarter of a second, then a second one holding
about six seconds of a sound of its own, whose pitch moves between 200 and
730 Hz. It is not the recording played back: the firmware plays a sound it
carries.

## Consequences

**Good**

- A firmware's microphone hears what a person gives it, and what it plays
  can be listened to and measured.
- A sound a firmware makes, a tone, a ringtone or a voice prompt, is a file
  a test can measure: its length, its level, its frequency.
- A firmware built on ESP-IDF's own I2S driver can now record at all.

**Costs, accepted**

- The resampling is the plainest there is. A file at another rate than the
  stream's comes out with the aliasing that implies. Recording at the
  stream's rate avoids it.
- Silence played is silence written. A firmware that keeps its speaker
  stream running with nothing to say still fills the file at the stream's
  rate.
- A capture hears the file once per stream, not once per run. A firmware
  that stops and restarts its microphone hears the file again.
