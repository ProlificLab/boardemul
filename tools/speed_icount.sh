#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Prolific Lab
# SPDX-License-Identifier: Apache-2.0
# Emulator speed under -icount (ADR 0189): the guest does the same work in every
# run, so the host CPU seconds QEMU spends to reach the home screen measure the
# emulator; a busy host still spreads them (by a factor of two in ADR 0189).
#   bash tools/speed_icount.sh RUNS NAME=QEMU_BINARY...
set -u
RUNS=$1; shift
FW=firmware/firmware-t-deck-tft-2.7.10.94d4bdf.bin
for run in $(seq 1 "$RUNS"); do
  for variant in "$@"; do
    name=${variant%%=*}; bin=${variant#*=}
    out=/tmp/si-$name-$run; rm -rf "$out"; mkdir -p "$out"
    start=$(date +%s.%N)
    BOARDEMUL_QEMU=$bin python3 emu/run.py "$FW" --board tdeck --bus \
        --timeout 900 --stall 900 --outdir "$out" > "$out.log" 2>&1 &
    runner=$!
    pid=""
    while kill -0 $runner 2>/dev/null; do
      [ -z "$pid" ] && pid=$(pgrep -f "qemu-system-xtensa.*$out" | head -1)
      uart=$(ls "$out"/*.uart.log 2>/dev/null | head -1)
      if [ -n "$uart" ] && [ "$(grep -a -c 'uiconfig.proto' "$uart")" -ge 2 ]; then
        break
      fi
      sleep 0.5
    done
    wall=$(awk -v a="$(date +%s.%N)" -v b="$start" 'BEGIN{print a-b}')
    cpu="-"
    if [ -n "$pid" ] && [ -r /proc/$pid/stat ]; then
      ticks=$(awk '{print $14 + $15}' /proc/$pid/stat)
      cpu=$(awk -v t="$ticks" -v h="$(getconf CLK_TCK)" 'BEGIN{printf "%.1f", t/h}')
    fi
    guest=$(grep -a 'uiconfig.proto' "$uart" 2>/dev/null | tail -1 | grep -a -o '^\[ *[0-9]*\]' | tr -d '[] ')
    printf '%-10s run %s: home at guest %6s ms, wall %6.1f s, qemu cpu %6s s\n' \
        "$name" "$run" "${guest:--}" "$wall" "$cpu"
    kill $runner 2>/dev/null; pkill -f "qemu-system-xtensa.*$out" 2>/dev/null; wait $runner 2>/dev/null
  done
done
