#!/usr/bin/env bash
# Re-test ADR 0007 on the QEMU tag of ADR 0138: which PSRAM line mode each
# firmware enumerates its PSRAM in. Run in the build container from /work.
set -u
run() {  # name board firmware mode
    local name=$1 board=$2 fw=$3 mode=$4
    local out=/tmp/psram/$name-$mode
    local flag=()
    [[ $mode == octal ]] && flag=(--psram-octal)
    rm -rf "$out"
    timeout 200 python3 emu/run.py "$fw" --board "$board" --bus "${flag[@]}" \
        --timeout 45 --stall 20 --outdir "$out" > "$out.log" 2>&1
    echo "== $name $mode"
    cat "$out"/*.uart.log "$out"/*.usb.log 2>/dev/null | tr -d '\r' \
        | grep -a -i -E "psram|spiram|Guru|panic|IntegerDivide" | sort | uniq -c | head -8
}
mkdir -p /tmp/psram
for mode in quad octal; do
    run max tdeck-max firmware/T-Deck-MAX_V3.1_20260313.bin $mode &
    run ut250620 tdeck firmware/T-Deck_UnitTest_250620.bin $mode &
    run ut251113 tdeck firmware/T-Deck_UnitTest_251113.bin $mode &
    run dbg251222 tdeck firmware/T-Deck_UnitTest_Debug_251222.bin $mode &
    run mesh tdeck firmware/firmware-t-deck-tft-2.7.10.94d4bdf.bin $mode &
done
wait
