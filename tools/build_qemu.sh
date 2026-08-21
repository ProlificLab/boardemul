#!/usr/bin/env bash
#
# Fetch and build the pinned Espressif QEMU fork. See ADR 0001 and ADR 0002.
#
set -euo pipefail

# Bumping this is an ADR-level decision, not a routine update. The whole point
# of ADR 0001's boundary rule is that this bump stays a two-line diff; if it
# ever costs more than that, the boundary has been breached.
QEMU_TAG="esp-develop-9.2.2-20260417"
QEMU_REPO="https://github.com/espressif/qemu.git"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_ROOT}/third_party/qemu"
BUILD="${SRC}/build"

if [[ ! -d "${SRC}/.git" ]]; then
    echo "==> cloning ${QEMU_TAG}"
    mkdir -p "$(dirname "${SRC}")"
    # Deliberately not --recurse-submodules. QEMU's submodules are almost
    # all ROM sources -- edk2 alone drags in its own openssl, mbedtls and
    # brotli -- for firmware targets we never build. configure pulls the few
    # subprojects xtensa-softmmu actually needs.
    git clone --depth 1 --branch "${QEMU_TAG}" "${QEMU_REPO}" "${SRC}"
else
    # An unexpected tag here means someone edited the tree in place, which
    # ADR 0001 forbids. Fail loudly rather than build something unidentifiable.
    have="$(git -C "${SRC}" describe --tags --exact-match 2>/dev/null || echo '<none>')"
    if [[ "${have}" != "${QEMU_TAG}" ]]; then
        echo "ERROR: ${SRC} is at '${have}', expected '${QEMU_TAG}'." >&2
        echo "       Remove third_party/qemu and re-run to get a clean tree." >&2
        exit 1
    fi
    echo "==> reusing existing clone at ${QEMU_TAG}"
fi

# ADR 0005: our single C file lives in this repo and is installed into the QEMU
# tree here, so the tree is never edited by hand and `git status` inside it stays
# clean apart from these two touches.
echo "==> installing esp_bus_bridge.c"
cp "${REPO_ROOT}/qemu-patch/esp_bus_bridge.c" "${SRC}/hw/misc/esp_bus_bridge.c"
MESON_FILE="${SRC}/hw/misc/meson.build"
if ! grep -qF "esp_bus_bridge.c" "${MESON_FILE}"; then
    echo "system_ss.add(files('esp_bus_bridge.c'))" >> "${MESON_FILE}"
fi

if [[ ! -f "${BUILD}/build.ninja" ]]; then
    echo "==> configuring"
    mkdir -p "${BUILD}"
    # --enable-trace-backends=log is pinned rather than left to the default
    # because emu/probe.py reads `-d trace:...` output (ADR 0002, decision 4).
    ( cd "${BUILD}" && ../configure \
        --target-list=xtensa-softmmu \
        --disable-werror \
        --enable-trace-backends=log )
fi

echo "==> building with $(nproc) jobs"
ninja -C "${BUILD}"

BIN="${BUILD}/qemu-system-xtensa"
echo
echo "==> verifying"
"${BIN}" --version | head -1
# If the esp32s3 machine is missing we built the wrong thing, and every later
# failure would look like a firmware problem instead of a build problem.
if ! "${BIN}" -machine help | grep -q '^esp32s3 '; then
    echo "ERROR: built QEMU has no esp32s3 machine." >&2
    exit 1
fi
echo "esp32s3 machine present"
echo
echo "binary: ${BIN}"
