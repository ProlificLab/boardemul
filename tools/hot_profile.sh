#!/usr/bin/env bash
# Where a firmware spends its guest time (ADR 0161). Run in the build
# container from /work:
#
#   bash tools/hot_profile.sh [SECONDS] [FIRMWARE] [BOARD]
#
# Boots the firmware for SECONDS of wall time twice, once with QEMU's
# hotblocks plugin (the most executed translation blocks, by guest address)
# and once with hwprofile (reads and writes per device region), and prints
# the head of each. A guest that polls a register in a loop shows up in both:
# a short block at the top of the first, a device at the top of the second.
set -u
SECONDS_WALL=${1:-90}
FW=${2:-firmware/firmware-t-deck-tft-2.7.10.94d4bdf.bin}
BOARD=${3:-tdeck}
PLUGINS=third_party/qemu/build/contrib/plugins
for plugin in hotblocks hwprofile; do
  out=/tmp/hot-$plugin
  rm -rf "$out"
  BOARDEMUL_QEMU_DFLAGS=plugin \
  BOARDEMUL_QEMU_ARGS="-plugin $PLUGINS/lib$plugin.so" \
    timeout $((SECONDS_WALL + 60)) python3 emu/run.py "$FW" \
      --board "$BOARD" --bus --timeout "$SECONDS_WALL" --stall "$SECONDS_WALL" \
      --outdir "$out" > "$out.log" 2>&1
  echo "== $plugin"
  # The plugin writes its report into QEMU's log when the guest stops.
  grep -a -A 25 -m 1 -E '^(collected|pc, tcount|counts|addr)' "$out"/*.qemu.log \
    || tail -n 30 "$out"/*.qemu.log
done
