#!/usr/bin/env bash
#
# Fetch and build the pinned Espressif QEMU fork. See ADR 0001 and ADR 0002.
#
set -euo pipefail

# Bumping this is an ADR-level decision, not a routine update. The whole point
# of ADR 0001's boundary rule is that this bump stays a two-line diff; if it
# ever costs more than that, the boundary has been breached.
#
# The tag and the commit it names, together (ADR 0138): the tag says which
# release, and the commit makes a tag that has moved an error rather than a
# different build.
QEMU_TAG="esp-develop-9.2.2-20261109"
QEMU_SHA="7411393a2f61e6f0b05af00276d30780769e0839"
QEMU_REPO="https://github.com/espressif/qemu.git"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${REPO_ROOT}/third_party/qemu"
BUILD="${SRC}/build"

sha_of() {
    git -C "${SRC}" rev-parse HEAD 2>/dev/null
}

if [[ ! -d "${SRC}/.git" ]]; then
    echo "==> cloning ${QEMU_TAG}"
    mkdir -p "$(dirname "${SRC}")"
    # Deliberately not --recurse-submodules. QEMU's submodules are almost
    # all ROM sources -- edk2 alone drags in its own openssl, mbedtls and
    # brotli -- for firmware targets we never build. configure pulls the few
    # subprojects xtensa-softmmu actually needs.
    git clone --depth 1 --branch "${QEMU_TAG}" "${QEMU_REPO}" "${SRC}"
    if [[ "$(sha_of)" != "${QEMU_SHA}" ]]; then
        echo "ERROR: ${QEMU_TAG} names $(sha_of), not ${QEMU_SHA}: the tag moved." >&2
        echo "       Re-check the release before pinning anything else." >&2
        exit 1
    fi
else
    # An unexpected SHA here means someone edited the tree in place, which
    # ADR 0001 forbids. Fail loudly rather than build something unidentifiable.
    have="$(sha_of)"
    if [[ "${have}" != "${QEMU_SHA}"* ]]; then
        echo "ERROR: ${SRC} is at '${have}', expected ${QEMU_TAG} (${QEMU_SHA})." >&2
        echo "       Remove third_party/qemu and re-run to get a clean tree." >&2
        exit 1
    fi
    echo "==> reusing existing clone at ${QEMU_TAG}"
fi

# ADR 0005: our C sources live in this repo and are installed into the QEMU tree
# here, so that tree is never edited by hand and `git status` inside it shows
# only our added files plus the build lines that name them.
# Experiments: candidate changes that are not fixes yet. Never applied unless
# named, one per comma, and the build says which are on. See
# qemu-patch/experiments/README.md -- the point of the directory is that a
# negative result is recorded instead of reverted into nothing.
EXPERIMENTS="${BOARDEMUL_QEMU_EXPERIMENTS:-}"

# Anything applied last time and not asked for now comes back out, so that a
# build is what it says it is. Leaving one in would be exactly the undeclared
# drift ADR 0001 exists to prevent, and it would be invisible.
for PATCH in "${REPO_ROOT}"/qemu-patch/experiments/*.patch; do
    [[ -f "${PATCH}" ]] || continue
    name="$(basename "${PATCH}" .patch)"
    if [[ ",${EXPERIMENTS}," == *",${name},"* ]]; then
        continue
    fi
    if git -C "${SRC}" apply --reverse --check "${PATCH}" 2>/dev/null; then
        git -C "${SRC}" apply --reverse "${PATCH}"
        echo "==> reverted experiment ${name} (not asked for)"
    fi
done

if [[ -n "${EXPERIMENTS}" ]]; then
    IFS=',' read -ra WANTED <<< "${EXPERIMENTS}"
    for name in "${WANTED[@]}"; do
        PATCH="${REPO_ROOT}/qemu-patch/experiments/${name}.patch"
        if [[ ! -f "${PATCH}" ]]; then
            echo "ERROR: no experiment called '${name}'." >&2
            echo "       Available:" >&2
            ls -1 "${REPO_ROOT}/qemu-patch/experiments/"*.patch 2>/dev/null |
                sed 's#.*/##; s#\.patch$##; s#^#         #' >&2
            exit 1
        fi
        if git -C "${SRC}" apply --reverse --check "${PATCH}" 2>/dev/null; then
            echo "==> EXPERIMENT ${name} already applied"
        elif git -C "${SRC}" apply --check "${PATCH}" 2>/dev/null; then
            git -C "${SRC}" apply "${PATCH}"
            echo "==> EXPERIMENT ${name} applied -- this build is not a plain one"
        else
            echo "ERROR: experiment ${name} no longer applies to ${QEMU_TAG}." >&2
            exit 1
        fi
    done
fi

echo "==> installing BoardEmul sources"
cp "${REPO_ROOT}/qemu-patch/esp_board_glue.h" "${SRC}/include/hw/misc/"
MESON_FILE="${SRC}/hw/misc/meson.build"
for f in esp_bus_bridge esp_gpio_block esp_spi_master esp_i2c_master esp_i2s_master esp_ble_core esp_wifi_mac \
         esp_usb_serial_jtag esp_guest_clock; do
    cp "${REPO_ROOT}/qemu-patch/${f}.c" "${SRC}/hw/misc/${f}.c"
    if ! grep -qF "${f}.c" "${MESON_FILE}"; then
        echo "system_ss.add(files('${f}.c'))" >> "${MESON_FILE}"
    fi
done

# ADR 0013: this one touches CPUXtensaState, so it must be compiled as a
# target-specific source and cannot sit in hw/misc with the others.
cp "${REPO_ROOT}/qemu-patch/esp_coproc_enable.c" "${SRC}/hw/xtensa/esp_coproc_enable.c"
XTENSA_MESON="${SRC}/hw/xtensa/meson.build"
if ! grep -qF "esp_coproc_enable.c" "${XTENSA_MESON}"; then
    LINE="xtensa_ss.add(when: 'CONFIG_XTENSA_ESP32S3', if_true: files('esp_coproc_enable.c'))"
    # Inserted before the hw_arch aggregation rather than appended after it,
    # because meson has already consumed the source set by that point.
    awk -v line="$LINE" '/^hw_arch \+=/ && !seen { print line; seen=1 } { print }' \
        "${XTENSA_MESON}" > "${XTENSA_MESON}.tmp"
    mv "${XTENSA_MESON}.tmp" "${XTENSA_MESON}"
fi

# The carried patches: the only places we change QEMU's own logic rather than
# adding to it. Each is reported upstream and recorded in an ADR; when one
# lands, delete the patch and supersede its ADR rather than carrying it on.
#   icount-sleep-off-deterministic.patch  ADR 0115  QEMU core: gitlab qemu-project/qemu 4632
#   flash-writes-synchronously.patch      ADR 0115  issue 188
# Nine more were carried until Espressif took them into esp-develop-9.2.2-20261109
# (ADRs 0017, 0022, 0023, 0066, 0083, 0101, 0105, 0122, 0123; dropped by
# ADR 0138). When one of these two lands and the pinned tag moves past it,
# delete the patch and say so in its ADR rather than carrying it on.
# Applied from patch files rather than by hand so that they are declared,
# reviewed like anything else, and fail loudly if a tag bump moves the code.
for name in icount-sleep-off-deterministic.patch flash-writes-synchronously.patch; do
    PATCH="${REPO_ROOT}/qemu-patch/${name}"
    if git -C "${SRC}" apply --reverse --check "${PATCH}" 2>/dev/null; then
        echo "==> ${name} already applied"
    elif git -C "${SRC}" apply --check "${PATCH}" 2>/dev/null; then
        git -C "${SRC}" apply "${PATCH}"
        echo "==> applied ${name}"
    else
        echo "ERROR: ${name} no longer applies to ${QEMU_TAG}." >&2
        echo "       Re-check it against the new source before going further." >&2
        exit 1
    fi
done

if [[ ! -f "${BUILD}/build.ninja" ]]; then
    echo "==> configuring"
    mkdir -p "${BUILD}"
    # --enable-trace-backends=log is pinned rather than left to the default
    # because emu/probe.py reads `-d trace:...` output (ADR 0002, decision 4).
    ( cd "${BUILD}" && ../configure \
        --target-list=xtensa-softmmu \
        --disable-werror \
        --enable-trace-backends=log \
        --enable-gcrypt )
    # --enable-gcrypt is explicit: our esp_wifi_mac.c links gcrypt directly
    # for CCMP (ADRs 0092, 0094), and on a host that happens to carry gnutls,
    # configure's auto choice would skip gcrypt and the link would fail.
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
