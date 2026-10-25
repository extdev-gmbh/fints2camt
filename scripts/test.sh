#!/bin/sh
# Run the test suite in a container: python:3.12-slim, the runtime dependencies from the
# hashed lock file requirements.txt (the versions the image ships) and pytest from the `dev`
# extra — what the CI test jobs install. Byte-compiles src and tests, then runs pytest.
#
# Needs nothing on the host besides Docker (the host has no pytest; never use the host's
# python3 for the gate). Tests the checkout it is called from — the repository root or any
# git worktree — and leaves nothing behind in it: the checkout is
# mounted read-only, the container runs as the calling user, caches live inside the
# container, which is removed after the run. Safe to run in several worktrees at once.
#
#   scripts/test.sh                              # the whole gate, as in CI
#   scripts/test.sh tests/test_smoke.py -k x     # arguments go to pytest
#
# The dependency image is built once per content of pyproject.toml and requirements.txt;
# TEST_IMAGE_REBUILD=1
# forces a fresh build. The package itself is not installed: pytest finds it via
# `pythonpath = ["src"]` (pyproject.toml), so the read-only checkout is what is tested.
set -eu

script_root="$(cd "$(dirname "$0")/.." && pwd)"

checkout="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$checkout" ] || [ ! -f "$checkout/pyproject.toml" ]; then
    checkout="$script_root"
fi

fingerprint="$(cat "$checkout/pyproject.toml" "$checkout/requirements.txt" | sha256sum | cut -c1-16)"
image="fints2camt-test:$fingerprint"

build() {
    context="$(mktemp -d)"
    cp "$checkout/pyproject.toml" "$checkout/requirements.txt" "$context/"
    cat > "$context/Dockerfile" <<'DOCKERFILE'
FROM python:3.12-slim
COPY pyproject.toml requirements.txt /tmp/
RUN python - <<'PY' > /tmp/dev-requirements.txt
import tomllib
p = tomllib.load(open("/tmp/pyproject.toml", "rb"))["project"]
print("\n".join(p.get("optional-dependencies", {}).get("dev", [])))
PY
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes --no-deps -r /tmp/requirements.txt \
    && pip install --no-cache-dir --disable-pip-version-check -r /tmp/dev-requirements.txt
DOCKERFILE
    docker build --quiet "$@" --tag "$image" "$context" >/dev/null
    rm -rf "$context"
}

if [ -n "${TEST_IMAGE_REBUILD:-}" ]; then
    build --pull --no-cache
elif ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "test.sh: building the dependency image $image" >&2
    build
fi

echo "test.sh: testing $checkout" >&2

# A local .env (tokens) must not reach the tests — CI has none either.
hide_env=""
if [ -e "$checkout/.env" ]; then
    hide_env="--volume /dev/null:/src/.env:ro"
fi

tty=""
if [ -t 1 ]; then
    tty="--tty"
fi

# shellcheck disable=SC2086  # $tty and $hide_env are optional flags, split on purpose
exec docker run --rm --init $tty \
    --user "$(id -u):$(id -g)" \
    --volume "$checkout:/src:ro" \
    $hide_env \
    --workdir /src \
    --env HOME=/tmp \
    --env PYTHONPYCACHEPREFIX=/tmp/pycache \
    --env PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache" \
    "$image" sh -c 'python -m compileall -q src tests && python -m pytest "$@"' gate "$@"
