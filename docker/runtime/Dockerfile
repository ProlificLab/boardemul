# The runtime image: QEMU already built, for people who run BoardEmul rather
# than develop it (ADR 0159). Build it from the repository's root:
#
#   docker build -t boardemul-runtime -f docker/runtime/Dockerfile .
#
# or let `python boardemul.py` do it once. The development image
# (.devcontainer/Dockerfile) and tools/build_qemu.sh are unchanged (ADR 0002).

# -- the compile: the build image's dependencies and the same script ---------
FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04 AS build
RUN apt-get -o Acquire::Retries=5 update && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        build-essential ca-certificates curl git libglib2.0-dev libgcrypt20-dev \
        libpixman-1-dev libslirp-dev ninja-build pkg-config python3 python3-venv \
        xz-utils zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
COPY tools/build_qemu.sh /src/tools/build_qemu.sh
COPY qemu-patch /src/qemu-patch
# The same pin and patches as the development tree, built in the image's own
# file system, then installed into a staging prefix.
RUN bash /src/tools/build_qemu.sh \
    && DESTDIR=/stage ninja -C /src/third_party/qemu/build install > /dev/null

# -- what a run needs: Python, QEMU's libraries, and QEMU --------------------
# The libraries are what QEMU's configure found in the build stage and linked
# against, curl's block driver among them; `ldd` on the built binary is how
# this list was taken, and the check after the copy says by name if it falls
# behind. Listed here rather than derived, so that this layer stays cached
# when only QEMU is rebuilt.
FROM ubuntu:24.04
RUN apt-get -o Acquire::Retries=5 update && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        ca-certificates libcurl4t64 libgcrypt20 libglib2.0-0t64 libgnutls30t64 \
        libpixman-1-0 libslirp0 libzstd1 python3 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /stage/usr/local /usr/local
# run.py prefers this over a tree under third_party/ (find_qemu).
ENV BOARDEMUL_QEMU=/usr/local/bin/qemu-system-xtensa
RUN missing="$(ldd /usr/local/bin/qemu-system-xtensa | grep 'not found' || true)"; \
    if [ -n "$missing" ]; then \
        echo "QEMU needs libraries this stage does not install:"; echo "$missing"; exit 1; \
    fi \
    && qemu-system-xtensa -machine help | grep -q '^esp32s3 ' \
    && qemu-system-xtensa -device help | grep -q 'esp-core-slice' \
    && qemu-system-xtensa -device help | grep -q 'esp-usb-otg'
# What it was built from, so that a clone which has moved on rebuilds it:
# tools/doctor.py's sources_digest(), which boardemul.py passes (ADR 0159).
ARG BOARDEMUL_SOURCES=unknown
LABEL org.boardemul.sources=$BOARDEMUL_SOURCES
WORKDIR /work
