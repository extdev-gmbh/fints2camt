# Changelog

All notable changes to fints2camt are listed here. The project follows
[Semantic Versioning](https://semver.org/): until 1.0.0 a minor version may change the
configuration format or the command line; such changes are listed under **Changed**.

## [0.1.0] - unreleased

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
