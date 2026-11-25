# Changelog

All notable changes to fints2camt are listed here. The project follows
[Semantic Versioning](https://semver.org/): until 1.0.0 a minor version may change the
configuration format or the command line; such changes are listed under **Changed**.

## [Unreleased]

## [0.1.3] - 2026-11-22

Maintenance release of the dependencies, the base image and the release workflow. The
program and its configuration are unchanged.

### Changed

- Container base image `python:3.15-slim` (was `python:3.12-slim`, pinned by digest); the
  tests run on Python 3.15 too (`scripts/test.sh`, GitLab CI, GitHub Actions). Python 3.12 or
  newer remains the requirement for an installation outside the image.
- Runtime dependencies: `mt-940` 5.1.3, `anyio` 4.16.0 (lock file regenerated with hashes).
- Release workflow: `docker/login-action` 4.8.0 (pinned to its commit SHA).
- `enum-tools` stays at 0.12.0: `python-fints` 5.0.1 requires `enum-tools~=0.12.0`.

## [0.1.2] - 2026-11-21

### Fixed

- A booked entry dated after today (a weekend fee the bank books on the next business day) no
  longer makes the statement invalid: the statement period (`ToDtTm`) and the closing balance
  date extend to the latest booking date, so targets that check the period accept the file.
  The fetch range, the state, `MsgId`/`Stmt/Id`, the folder file name and the entry references
  stay keyed on the fetch window; the day is fetched again on the next run.

## [0.1.1] - 2026-10-25

Maintenance release of the build and the release workflow. The program, its configuration
and its runtime dependencies are unchanged.

### Added

- The release workflow can be started by hand for an existing release tag
  (`workflow_dispatch`), should GitHub not start it for a pushed tag.
- The tests check that the lock file satisfies the requirements of every locked package (the
  hashed install with `--no-deps` does not).

### Changed

- Release and test workflows: `actions/checkout` 7.0.1, `actions/setup-python` 7.1.0,
  `docker/setup-buildx-action` 4.4.1, `docker/build-push-action` 7.4.0, `docker/login-action`
  4.6.0 (Node 24 runtime; each pinned to its commit SHA).
- `enum-tools` stays at 0.12.0: `python-fints` 5.0.1 requires `enum-tools~=0.12.0`.
- A release arrives on GitHub as the release commit and then its tag in a separate push.

### Fixed

- README: `cosign verify` needs cosign 3.0 or newer (cosign 2 does not find the signature).
- The 0.1.0 section of this changelog is dated.

## [0.1.0] - 2026-10-25

First release.

### Added

- Fetch transactions via FinTS (`python-fints`): camt via `HKCAZ` when the bank offers it,
  otherwise MT940 via `HKKAZ`; balances from the statement data or `HKSAL`.
- camt.053.001.02 output validated against the ISO 20022 XSD: bank camt passed through or
  mapped, MT940 converted; a deterministic `NtryRef` per entry.
- Delivery per account by HTTPS `POST` with a bearer token, or into a local folder; the state
  advances only after a successful (2xx) delivery.
- Command line `setup`, `run`, `approve`, `check`; decoupled pushTAN without push spam; a
  wrong PIN stops at once and is never retried; exit codes for timers and monitoring;
  optional health-check pings.
- Container image `ghcr.io/extdev-gmbh/fints2camt` (amd64, arm64; non-root, read-only
  configuration and secrets), built by a GitHub Actions workflow on each release tag; a Docker
  Compose example, a systemd timer and a cron example.
- README with the security model, `LICENSE` (Apache-2.0), `NOTICE`, `SECURITY.md`.
- Lock-out protection beyond the recognised PIN errors: refused dialog starts are counted, and
  after two in a row runs stop contacting the bank until `approve`; one `run`/`setup`/`approve`
  at a time (a lock next to the state file).
- `token_file` for target tokens; relative `pin_file`/`token_file` paths are relative to the
  configuration file. IBANs are masked in messages, XSD errors never quote statement content.
- Pinned build inputs (hashed lock file, base image by digest, actions by commit SHA,
  Dependabot); the image is signed with cosign and carries provenance and an SBOM; tests run
  on GitHub Actions and gate the release build; `:latest` only for the highest release.
