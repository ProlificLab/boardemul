#!/usr/bin/env bash
# How fast Meshtastic boots on the T-Deck under different scheduling choices
# (ADR 0161). Run in the build container from /work:
#
#   bash tools/speed.sh [SECONDS] [RUNS] [VARIANT...]
#
# A VARIANT is NAME=ASSIGNMENTS, the environment that run gets, for example
#   'slice-off=BOARDEMUL_CORE_SLICE_US=0'  'multi=BOARDEMUL_TCG_THREADS=multi'
# The default compares QEMU's own 100 ms turn, the 1 ms and 200 us turns,
# and two translation threads. Runs are interleaved, so a machine that gets
# busier halfway through weighs on every variant alike.
#
# Without -icount the guest's clock is the host's, so a milestone's own
# timestamp is the wall time it took to reach it. Reported per run, in guest
# milliseconds (lower is faster): when the firmware looked for its boot logo,
# when it read its UI settings the second time (the home screen is up), and
# how many frames it drew in SECONDS.
set -u
SECONDS_WALL=${1:-90}
RUNS=${2:-2}
shift 2 2>/dev/null || shift $#
if [ $# -eq 0 ]; then
  set -- 'slice-off=BOARDEMUL_CORE_SLICE_US=0' 'slice-1ms=BOARDEMUL_CORE_SLICE_US=1000' \
         'slice-200us=BOARDEMUL_CORE_SLICE_US=200' 'multi=BOARDEMUL_TCG_THREADS=multi'
fi
FW=firmware/firmware-t-deck-tft-2.7.10.94d4bdf.bin
millis_of() {   # the bracketed millis of the LAST line matching $1 in $2
  grep -a "$1" "$2" | tail -1 | grep -a -o '^\[ *[0-9]*\]' | tr -d '[] '
}
for run in $(seq 1 "$RUNS"); do
  for variant in "$@"; do
    name=${variant%%=*}
    assignments=${variant#*=}
    out=/tmp/speed-$name-$run
    rm -rf "$out"
    env $assignments timeout $((SECONDS_WALL + 60)) python3 emu/run.py "$FW" \
        --board tdeck --bus --timeout "$SECONDS_WALL" --stall "$SECONDS_WALL" \
        --outdir "$out" > "$out.log" 2>&1
    uart=$(ls "$out"/*.uart.log 2>/dev/null | head -1)
    logo=$(millis_of 'boot/logo.png' "$uart")
    home=$(millis_of 'uiconfig.proto' "$uart")
    frames=$(ls "$out/frames" 2>/dev/null | grep -c '^frame-')
    crashed=$(grep -a -c -i 'segmentation\|core dumped' "$out.log" "$out"/*.stderr.log 2>/dev/null |
              awk -F: '{s+=$2} END {print s+0}')
    printf '%-12s run %s: logo %6s ms, home %6s ms, %3s frames, crash lines %s\n' \
        "$name" "$run" "${logo:--}" "${home:--}" "$frames" "$crashed"
  done
done
