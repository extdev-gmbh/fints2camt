# fints2camt

`fints2camt` is a small, generic connector that fetches the transactions of your own bank accounts via
FinTS (with your own online-banking access), produces camt.053 statements and delivers them by HTTPS
POST to a configurable target or into a local folder. It runs on the account holder's own
infrastructure as a one-shot job started by a timer: no web UI, no open port, no daemon, no telemetry —
PIN and TAN never leave the operator's host except towards the bank.

*Deutsch: [Einrichtung für Praxen und kleine Unternehmen in fünf Schritten](#einrichtung-in-fünf-schritten).*

- [What it does](#what-it-does)
- [Security model](#security-model)
- [Install](#install)
- [Setup and approval](#setup-and-approval)
- [Commands, exit codes, log](#commands-exit-codes-log)
- [Configuration reference](#configuration-reference)
- [Your own FinTS product id](#your-own-fints-product-id)
- [Versions and releases](#versions-and-releases)
- [Einrichtung in fünf Schritten](#einrichtung-in-fünf-schritten)

## What it does

- **Fetch via FinTS** (`python-fints`): camt via `HKCAZ` when the bank offers it, otherwise MT940 via
  `HKKAZ`; balances from the statement data or `HKSAL`.
- **camt.053.001.02 output**, validated against the ISO 20022 XSD: the bank's camt is passed through
  (camt.052/053 `*.001.02` entries unchanged, other versions mapped), MT940 is converted (booking and
  value date, amount and direction, counterparty name/IBAN/BIC, remittance, end-to-end id, mandate and
  creditor id, bank transaction code, batch entries, reversals). Every entry carries a deterministic
  `NtryRef`, so re-fetched overlapping days can be deduplicated by the target.
- **Delivery** per account: `POST` to an HTTPS target with `Authorization: Bearer <token>` and
  `Content-Type: application/xml`, the camt.053 as body; retried with a doubling backoff on network
  errors and HTTP 5xx, never on 4xx; redirects are not followed. Or a file per account and period in
  a local folder (`<IBAN>_<from>_<to>.camt053.xml`).
- **State** in a local file (last delivered booking date per account, FinTS client data, the selected
  TAN method; never the PIN). An account's date advances only after its delivery succeeded (2xx).
  Each run fetches from the last delivered date minus an overlap (default 3 days); the first run from
  a configurable date.

## Security model

fints2camt is software you run, not a service: it works on the account holder's own host with the
holder's own online-banking access, and nobody else — including its authors — has a part in it.
These rules are part of the design and are not configurable:

- **No web UI, no open port, no daemon.** One run is fetch → convert → deliver → log → exit. The
  container exposes no port and runs nothing in the background; a systemd timer or cron starts it.
  Only `setup` and `approve` are interactive.
- **PIN and TAN stay with the operator.** The PIN is read from an environment variable or a secret
  file on the host at the start of a run (`check` reads it too, to report whether it is set) and used
  only for the bank dialog; it is sent to nowhere but the bank and never logged, printed or written
  to the state file. The TAN is approved by the operator in the banking
  app (pushTAN) or typed into `setup`/`approve`.
- **Outbound only, and only what the configuration names.** fints2camt connects to the bank's FinTS
  URL, to the configured delivery targets and, if configured, to the health-check URLs — nothing else.
  It delivers exactly the accounts the configuration lists. All URLs must be `https://`; redirects
  are not followed. The standard proxy and certificate variables of the environment are honoured
  (`HTTPS_PROXY`/`NO_PROXY`, `SSL_CERT_FILE`/`SSL_CERT_DIR`; for the bank dialog, which uses
  `requests`, also `REQUESTS_CA_BUNDLE`): set them only for a proxy or CA you mean to use.
- **No telemetry, no remote control, no auto-update.** Nothing reports home, nothing can be switched
  on remotely; a new version is installed only when the operator pulls or builds it.
- **No lock-out, no push spam.** A rejected PIN stops at once and is never retried (repeated attempts
  would lock the online-banking access). Because banks answer a wrong PIN with various codes, every
  refused start of the bank dialog counts as well: after two in a row, runs stop contacting the bank
  (exit code 4) until `approve` succeeds; a network error does not count. An approval that did not
  arrive is not requested again by the next runs until the operator runs `approve`. Only one
  `run`, `setup` or `approve` works at a time (a lock next to the state file): one started while
  another is running exits at once with code 1 instead of opening a second dialog.
- **The FinTS product id is the operator's.** It is not part of this repository; each organisation
  that runs fints2camt uses its own registration (see [below](#your-own-fints-product-id)).
- **Least privilege in the container.** The image runs as an unprivileged user (uid 10001); the
  configuration and the PIN file are mounted read-only; the only writable place is the state volume
  (and a folder target, if one is configured).

The state file (mode 0600) contains the FinTS client data, which includes account numbers: keep the
state volume as private as the PIN. Messages and logs show IBANs masked (`DE89…3000`); a folder
target's file names contain the full IBAN. Report vulnerabilities as described in
[SECURITY.md](SECURITY.md).

## Install

### Container (recommended)

Each release is published as a container image for amd64 and arm64 (e.g. a Raspberry Pi or an
ARM VPS) at `ghcr.io/extdev-gmbh/fints2camt`, tagged with the release (`:v0.1.0`); `:latest` is the
highest release:

```sh
docker pull ghcr.io/extdev-gmbh/fints2camt:v0.1.0
```

Pin a release tag rather than `:latest`, so that a new version is installed only when you change
the tag. The image is built by the repository's GitHub Actions workflow
(`.github/workflows/release.yml`) from the tagged source; its labels name the source repository,
the version and the licence. Its entrypoint is `fints2camt` (as `python -m fints2camt`), the default
command `run`. Every input of the build is pinned: the base image by digest, all Python dependencies
(transitive ones included) by version and hash (`requirements.txt`), the workflow's actions by commit.

Each image is signed (keyless, [Sigstore cosign](https://docs.sigstore.dev/)) by the release workflow
and carries its build provenance and an SBOM. To check that an image was built by this repository's
release workflow from a release tag:

```sh
cosign verify ghcr.io/extdev-gmbh/fints2camt:v0.1.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp '^https://github\.com/extdev-gmbh/fints2camt/\.github/workflows/release\.yml@refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$'
docker buildx imagetools inspect ghcr.io/extdev-gmbh/fints2camt:v0.1.0 --format '{{ json .Provenance }}'
```

Alternatively, build the image yourself from a release tag:

```sh
git clone https://github.com/extdev-gmbh/fints2camt.git && cd fints2camt
git checkout v0.1.0                      # a release tag
docker build -t fints2camt:0.1.0 .
```

It also cross-builds without emulation: `docker buildx build --platform linux/amd64,linux/arm64 ...`.
In `docker-compose.yml`, replace `image:` by your tag, or by `build: <path to the checkout>`.

Then create a directory for the installation, e.g. `/opt/fints2camt`:

```
/opt/fints2camt/
├── docker-compose.yml     # from deploy/docker-compose.example.yml
├── config.toml            # see the configuration reference; state_file = "/state/state.json"
├── fints2camt.env         # FINTS_PRODUCT_ID=… (and target tokens, unless token_file), chmod 400
└── secrets/fints_pin      # the online-banking PIN, chmod 400 (owner 10001, see below)
```

The PIN file must be readable by the container's user only:
`sudo chown 10001:10001 secrets/fints_pin && sudo chmod 400 secrets/fints_pin`. A target token can
be such a file as well (`token_file`), the same way. Compose passes the values of `fints2camt.env`
into the container's environment, where `docker inspect` shows them to everyone who may use the
Docker daemon; a secret file mounted read-only is not shown there.

`deploy/` holds the examples: `docker-compose.example.yml` (read-only container, configuration and
PIN mounted read-only, a named volume for the state), `systemd/fints2camt.service` +
`systemd/fints2camt.timer` (one run every morning) and `cron.example` (the same with cron).

```sh
sudo cp deploy/systemd/fints2camt.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now fints2camt.timer
journalctl -u fints2camt.service          # the runs' summary lines and details
```

A folder target needs a host directory mounted into the container that uid 10001 may write to.

### Without a container

Python 3.12 or newer:

```sh
python3 -m venv /opt/fints2camt/venv
/opt/fints2camt/venv/bin/pip install ./fints2camt     # a checkout of a release tag
/opt/fints2camt/venv/bin/fints2camt --version
```

Start `fints2camt run` from a timer as an unprivileged user with `FINTS_PRODUCT_ID`, the PIN and the
tokens in its environment (e.g. systemd `EnvironmentFile=` with mode 0400), or the PIN and the
tokens in secret files (`pin_file`, `token_file`) readable by that user only.

## Setup and approval

```sh
docker compose run --rm fints2camt check     # configuration, secrets, state, bank reachable?
docker compose run --rm fints2camt setup     # once: first login, TAN method, first approval
docker compose run --rm fints2camt run       # a first run by hand
```

`setup` logs in, offers the bank's TAN methods (preselecting a decoupled one: pushTAN, the approval in
the banking app), asks for the first approval and stores the FinTS registration in the state. From
then on the timer's `run` needs no interaction — until the bank asks for a new strong customer
authentication, usually every 90 days. A `run` that meets that request waits up to
`[tan] wait_minutes` for the approval in the app; if it does not come, the run exits with code 3
(„approval needed – run `fints2camt approve`") and later runs do not contact the bank until

```sh
docker compose run --rm fints2camt approve
```

succeeded. So there is never more than one push notification per approval. When the bank rejects the
PIN — or refuses the start of the dialog twice in a row, which may be a wrong PIN the bank reports
with another code — runs stop with code 4 and do not try again: fix the PIN file, then run
`approve`.

## Commands, exit codes, log

```
fints2camt [--config FILE] setup     # interactive: first login, TAN method, first approval
fints2camt [--config FILE] run       # the scheduled run: fetch → convert → deliver → exit
fints2camt [--config FILE] approve   # interactive: approve the bank access again
fints2camt [--config FILE] check     # validate configuration, secrets, state and reachability
fints2camt --version
```

The configuration file defaults to `$FINTS2CAMT_CONFIG` (in the image: `/config/config.toml`), else
`config.toml`. `--verbose` logs debug details (never secrets). `check` opens no FinTS dialog: no PIN
is sent, no TAN asked for, nothing delivered.

`run` prints one summary line on stdout (`fints2camt run: ok – 2/2 accounts delivered, 17 entries`),
details on stderr, and exits with

| Code | Meaning |
|---|---|
| 0 | every account delivered |
| 1 | an account was not fetched or delivered (the others were), another `fints2camt` is running, or another error |
| 2 | configuration, secret or `FINTS_PRODUCT_ID` missing or invalid |
| 3 | operator action needed: run `fints2camt approve` (or `setup`) |
| 4 | the bank rejected the PIN, locked the access or refused two dialog starts in a row: fix the PIN, then `approve` |

Optionally, `[healthcheck] success_url` / `failure_url` are pinged (plain `GET`) after each run, e.g.
an Uptime Kuma push URL, so a missing or failed run is noticed.

## Configuration reference

The configuration is a TOML file. A complete example:

```toml
state_file = "/state/state.json"

[bank]
blz = "12345678"
url = "https://fints.example-bank.de/fints"
user_id = "your online-banking login"
pin_file = "/run/secrets/fints_pin"

[fetch]
start_date = 2026-01-01

[[account]]
iban = "DE89 3704 0044 0532 0130 00"
target = { url = "https://accounting.example.org/camt", token_env = "TARGET_TOKEN" }

[[account]]
iban = "DE02 1203 0000 0000 2020 51"
target = { folder = "/data/camt" }
```

| Key | Default | Meaning |
|---|---|---|
| `state_file` | `state.json` | The state file; relative paths are relative to the configuration file. In the container: `/state/state.json`. The lock `<state_file>.lock` is created next to it. |
| `[bank] blz` | — | The bank's 8-digit bank code (BLZ). |
| `[bank] url` | — | The bank's FinTS URL (`https://`), from the bank or the DK's FinTS institute list. |
| `[bank] user_id` | — | The online-banking login (Anmeldename / Legitimations-ID). |
| `[bank] customer_id` | — | Only if the bank needs a customer id besides the login. |
| `[bank] bic` | — | Optional; written into the camt as the account servicer. |
| `[bank] pin_env` | `FINTS_PIN` | Environment variable holding the PIN. |
| `[bank] pin_file` | — | A file holding the PIN; takes precedence over `pin_env`. Relative paths are relative to the configuration file. |
| `[fetch] format` | `auto` | `auto` (camt via `HKCAZ` if the bank offers it, else MT940), `camt` or `mt940`. |
| `[fetch] overlap_days` | `3` | Each run re-fetches this many days before the last delivered date. |
| `[fetch] start_date` | 90 days back | First-run start date (`YYYY-MM-DD`, unquoted). |
| `[tan] wait_minutes` | `5` | How long `run` waits for a pushTAN approval (0–60). |
| `[tan] poll_seconds` | `5` | Interval of the status queries while waiting (1–300). |
| `[delivery] attempts` | `4` | Attempts per account, on network errors and HTTP 5xx only (1–10). |
| `[delivery] backoff_seconds` | `5` | Wait before the second attempt, doubled after each (0–600). |
| `[delivery] timeout_seconds` | `60` | HTTP timeout per attempt (1–600). |
| `[healthcheck] success_url` | — | `https://` URL pinged (`GET`) after a successful run. |
| `[healthcheck] failure_url` | — | `https://` URL pinged after a failed run. |
| `[[account]] iban` | — | One table per account to fetch; spaces are allowed. |
| `[[account]] currency` | `EUR` | The account currency (ISO 4217). |
| `[[account]] start_date` | `[fetch] start_date` | First-run start date of this account. |
| `[[account]] target` | — | Either `{ url = "https://…", token_env = "NAME" }` — `POST` with the bearer token from the environment variable `NAME` —, `{ url = "https://…", token_file = "/run/secrets/token" }` — the token from a file (relative: to the configuration file) — or `{ folder = "/path" }`. |

Environment variables: `FINTS_PRODUCT_ID` (required, see below), the PIN variable (`pin_env`) unless
`pin_file` is used, each target's `token_env` unless it uses `token_file`, and optionally
`FINTS2CAMT_CONFIG`.

## Your own FinTS product id

Every FinTS client software must send a product registration number, issued free of charge by the
Deutsche Kreditwirtschaft (DK). Banks may refuse unregistered clients. The number identifies the
organisation that runs the software, so it is **not part of this repository**: like `python-fints`,
fints2camt expects each operator to use their own.

1. Fill in the registration form of the DK's FinTS registration office:
   <https://www.hbci-zka.de/register/prod_register.htm> (product name e.g. „fints2camt", your
   organisation as manufacturer).
2. You receive the registration number by e-mail, usually within a few weeks.
3. Put it into the environment as `FINTS_PRODUCT_ID` (e.g. in `fints2camt.env`). Without it, `check`
   reports the missing variable and `run` exits with code 2.

Treat it like a credential of your organisation: do not publish it.

## Versions and releases

fints2camt uses [semantic versions](https://semver.org/); release tags are `vX.Y.Z`, and the changes
of each version are listed in [CHANGELOG.md](CHANGELOG.md). Until 1.0.0 a minor version may change
the configuration or the command line. Updating means changing the image tag in `docker-compose.yml`
(or checking out the new tag and rebuilding the image) — nothing updates itself.

Development: `scripts/test.sh` runs the tests in a `python:3.12-slim` container (needs only Docker);
GitHub Actions runs them on every push and pull request, and before every release build. Tests use
synthetic statements and mocked FinTS dialogs only. `requirements.txt` is the hashed lock file of
the runtime dependencies; after a change of the dependencies in `pyproject.toml`, regenerate it with
pip-tools (`pip-compile --generate-hashes --strip-extras --no-emit-index-url
--output-file=requirements.txt pyproject.toml`, Python 3.12 on Linux). Dependabot proposes updates
of the dependencies, the base image and the actions.

## Einrichtung in fünf Schritten

Für Praxen und kleine Unternehmen, die ihre Kontoumsätze automatisch als camt.053 an ihre
Buchhaltung übergeben möchten. fints2camt läuft auf Ihrem eigenen Server (z. B. einem kleinen VPS mit
Docker) mit Ihrem eigenen Online-Banking-Zugang. PIN und TAN bleiben bei Ihnen: die PIN liegt nur auf
Ihrem Server und geht ausschließlich an Ihre Bank; Freigaben erteilen Sie in Ihrer Banking-App.

1. **Produkt-ID beantragen.** Registrieren Sie fints2camt kostenlos bei der Deutschen
   Kreditwirtschaft (<https://www.hbci-zka.de/register/prod_register.htm>). Die Registrierungsnummer
   kommt per E-Mail; sie gehört in `fints2camt.env` als `FINTS_PRODUCT_ID=…`. Ihr IT-Dienstleister
   kann das für Sie übernehmen.
2. **Installieren.** Auf dem Server ein Verzeichnis
   `/opt/fints2camt` mit `docker-compose.yml` (Vorlage: `deploy/docker-compose.example.yml`)
   anlegen. Das Image `ghcr.io/extdev-gmbh/fints2camt` lädt Docker beim ersten Start selbst; die
   Vorlage nennt eine feste Version (siehe [Install](#install)).
3. **Konfigurieren.** In `config.toml` Bankleitzahl, FinTS-URL Ihrer Bank, Anmeldename, die IBANs
   der abzurufenden Konten und das Ziel (Adresse und Token Ihrer Buchhaltung, oder ein Ordner)
   eintragen. Die PIN kommt in die Datei `secrets/fints_pin` (nur für den Container lesbar:
   `chown 10001:10001` und `chmod 400`), das Token in `fints2camt.env` (`chmod 400`) oder ebenso
als Datei (`token_file`).
4. **Einmal freigeben.** `docker compose run --rm fints2camt check`, dann
   `docker compose run --rm fints2camt setup`: TAN-Verfahren wählen (pushTAN) und den Zugriff in der
   Banking-App freigeben.
5. **Zeitplan einschalten.** Den systemd-Timer aus `deploy/systemd/` installieren (oder die
   cron-Zeile aus `deploy/cron.example`). Ab dann werden die Umsätze jeden Morgen abgeholt und
   zugestellt.

Etwa alle 90 Tage verlangt die Bank eine neue Freigabe. Der Lauf meldet dann „approval needed"
(Exit-Code 3, auf Wunsch per Health-Check-Benachrichtigung) und fragt die Bank bis dahin nicht
erneut an — keine Flut von Push-Nachrichten. Führen Sie dann
`docker compose run --rm fints2camt approve` aus und bestätigen Sie in der App. Wird die PIN
abgelehnt, hält fints2camt sofort an, damit der Zugang nicht gesperrt wird: PIN-Datei korrigieren,
dann `approve`.

## License

Licensed under the [Apache License 2.0](LICENSE); third-party notices in [NOTICE](NOTICE).
