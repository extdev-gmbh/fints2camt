# fints2camt container image: one run per container start (fetch → convert → deliver → exit).
#
#   docker build -t fints2camt .
#   docker run --rm -v ./config.toml:/config/config.toml:ro -v fints2camt-state:/state \
#       -e FINTS_PRODUCT_ID -e FINTS_PIN fints2camt run
#
# No port is exposed and nothing runs in the background: the entrypoint is the CLI.
#
# Multi-arch without emulation: the build stage runs on the build machine's platform and
# installs the target platform's binary wheels (every dependency ships them for amd64 and
# arm64); the runtime stage only copies, so it has no RUN step that would need QEMU.
#   docker buildx build --platform linux/amd64,linux/arm64 .
#
# Reproducible inputs: the base image is pinned by digest (the multi-arch index of
# python:3.12-slim; Dependabot proposes updates), and every dependency, transitive ones
# included, is installed from requirements.txt by exact version and hash
# (--require-hashes --no-deps: nothing is resolved at build time). pip is the one of the
# pinned base image; it is not upgraded. fints2camt itself is pure Python and is copied, so
# no build backend is fetched either.

FROM --platform=$BUILDPLATFORM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS build
ARG TARGETARCH
WORKDIR /build
COPY requirements.txt LICENSE NOTICE ./
COPY src ./src
RUN set -eu; \
    case "$TARGETARCH" in \
        amd64) arch=x86_64 ;; \
        arm64) arch=aarch64 ;; \
        *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    pip install --no-cache-dir --no-compile --disable-pip-version-check --target /app \
        --require-hashes --no-deps -r requirements.txt \
        --only-binary=:all: --implementation cp --python-version 3.12 \
        --platform "manylinux2014_$arch" --platform "manylinux_2_17_$arch" \
        --platform "manylinux_2_28_$arch"; \
    cp -r src/fints2camt /app/fints2camt; \
    cp LICENSE NOTICE /app/; \
    rm -rf /app/bin; \
    python -m compileall -q /app; \
    mkdir -p /rootfs/etc /rootfs/state; \
    cp /etc/passwd /etc/group /rootfs/etc/; \
    echo "fints2camt:x:10001:10001:fints2camt:/state:/usr/sbin/nologin" >> /rootfs/etc/passwd; \
    echo "fints2camt:x:10001:" >> /rootfs/etc/group

# The same digest as the build stage (keep both FROM lines in step).
FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS runtime
LABEL org.opencontainers.image.title="fints2camt" \
      org.opencontainers.image.description="Fetch bank transactions via FinTS, produce camt.053 and deliver them" \
      org.opencontainers.image.licenses="Apache-2.0"
COPY --from=build /rootfs/etc/passwd /rootfs/etc/group /etc/
COPY --from=build --chown=10001:10001 --chmod=0700 /rootfs/state /state
COPY --from=build /app /opt/fints2camt
ENV FINTS2CAMT_CONFIG=/config/config.toml \
    PYTHONPATH=/opt/fints2camt \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /state
VOLUME ["/state"]
ENTRYPOINT ["python", "-m", "fints2camt"]
CMD ["run"]

# CI smoke test (`--target smoke`, build platform only): the image starts, as non-root.
FROM runtime AS smoke
RUN python -m fints2camt --version && test "$(id -u)" = 10001 && test -w /state

FROM runtime
