"""The installation's configuration file (TOML).

Example::

    state_file = "/var/lib/fints2camt/state.json"

    [bank]
    blz = "12345678"
    url = "https://fints.example-bank.de/fints"
    user_id = "online-banking login"
    customer_id = "only if the bank needs one besides the login"
    bic = "EXAMPLEBXXX"              # optional, written as the account servicer
    pin_env = "FINTS_PIN"            # default; or: pin_file = "/run/secrets/fints_pin"
                                     # (relative paths: relative to this file, like state_file)

    [fetch]
    format = "auto"                  # auto (camt via HKCAZ if offered) | camt | mt940
    overlap_days = 3
    start_date = 2026-01-01          # first run; default: 90 days back

    [tan]
    wait_minutes = 5                 # run: how long to wait for a decoupled (pushTAN) approval
    poll_seconds = 5                 # interval of the status queries while waiting

    [delivery]
    attempts = 4                     # per account, on network errors and HTTP 5xx only
    backoff_seconds = 5              # doubled after every failed attempt
    timeout_seconds = 60

    [healthcheck]                    # optional, e.g. an Uptime Kuma push URL
    success_url = "https://uptime.example.org/api/push/abc?status=up"
    failure_url = "https://uptime.example.org/api/push/abc?status=down"

    [[account]]
    iban = "DE89 3704 0044 0532 0130 00"
    target = { url = "https://accounting.example.org/camt", token_env = "TARGET_TOKEN" }
    # or, with the token in a secret file: token_file = "/run/secrets/target_token"

    [[account]]
    iban = "DE02 1203 0000 0000 2020 51"
    start_date = 2026-06-01          # optional per-account first-run start
    target = { folder = "/data/camt" }

The PIN and the target tokens are never part of the configuration: they are read from the
environment variables or the secret files the configuration names, at the start of a command.
"""

from __future__ import annotations

import datetime
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import ConfigError

DEFAULT_OVERLAP_DAYS = 3
DEFAULT_FIRST_RUN_DAYS = 90
DEFAULT_TAN_WAIT_MINUTES = 5
DEFAULT_TAN_POLL_SECONDS = 5
DEFAULT_DELIVERY_ATTEMPTS = 4
DEFAULT_DELIVERY_BACKOFF_SECONDS = 5
DEFAULT_DELIVERY_TIMEOUT_SECONDS = 60
FetchFormat = Literal["auto", "camt", "mt940"]

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BLZ = re.compile(r"^\d{8}$")
_BIC = re.compile(r"^[A-Z]{6}[A-Z2-9][A-NP-Z0-9]([A-Z0-9]{3})?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class BankConfig:
    blz: str
    url: str
    user_id: str
    customer_id: str | None = None
    bic: str | None = None
    pin_env: str = "FINTS_PIN"
    pin_file: Path | None = None

    def __repr__(self) -> str:
        return f"BankConfig(blz={self.blz!r}, url={self.url!r})"


@dataclass(frozen=True)
class TargetConfig:
    """Where the camt.053 of an account goes: an HTTPS endpoint or a local folder.

    The bearer token of an HTTPS target comes from ``token_env`` or ``token_file``.
    """

    url: str | None = None
    token_env: str | None = None
    folder: Path | None = None
    token_file: Path | None = None

    @property
    def token_source(self) -> str:
        """Where the token is read from, for messages (never the token)."""
        if self.token_file is not None:
            return f"token file {self.token_file}"
        return f"token {self.token_env}"


@dataclass(frozen=True)
class AccountConfig:
    iban: str
    target: TargetConfig
    currency: str = "EUR"
    start_date: datetime.date | None = None


@dataclass(frozen=True)
class FetchConfig:
    format: FetchFormat = "auto"
    overlap_days: int = DEFAULT_OVERLAP_DAYS
    start_date: datetime.date | None = None


@dataclass(frozen=True)
class TanConfig:
    """How ``run`` handles a strong customer authentication the bank asks for."""

    wait_minutes: int = DEFAULT_TAN_WAIT_MINUTES
    poll_seconds: int = DEFAULT_TAN_POLL_SECONDS


@dataclass(frozen=True)
class DeliveryConfig:
    attempts: int = DEFAULT_DELIVERY_ATTEMPTS
    backoff_seconds: int = DEFAULT_DELIVERY_BACKOFF_SECONDS
    timeout_seconds: int = DEFAULT_DELIVERY_TIMEOUT_SECONDS


@dataclass(frozen=True)
class HealthcheckConfig:
    """URLs pinged (GET) after a run; either may be unset."""

    success_url: str | None = None
    failure_url: str | None = None

    def __repr__(self) -> str:
        # push URLs usually carry a secret token
        return "HealthcheckConfig(...)"


@dataclass(frozen=True)
class Config:
    bank: BankConfig
    accounts: tuple[AccountConfig, ...]
    fetch: FetchConfig
    state_file: Path
    tan: TanConfig = TanConfig()
    delivery: DeliveryConfig = DeliveryConfig()
    healthcheck: HealthcheckConfig = HealthcheckConfig()


def load_config(path: Path) -> Config:
    """Read and validate the configuration file."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"configuration file {path} is not valid TOML: {exc}") from exc
    return parse_config(data, base_dir=path.parent)


def parse_config(data: Mapping[str, Any], base_dir: Path = Path(".")) -> Config:
    bank = _bank(_table(data, "bank"), base_dir)
    fetch = _fetch(_table(data, "fetch", required=False))
    raw_accounts = data.get("account")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ConfigError("the configuration needs at least one [[account]]")
    accounts = tuple(_account(raw, index, base_dir) for index, raw in enumerate(raw_accounts, start=1))
    ibans = [account.iban for account in accounts]
    if len(set(ibans)) != len(ibans):
        raise ConfigError("an IBAN is configured more than once")
    state_file = _path(_string(data, "state_file", "", default="state.json"), base_dir)
    return Config(
        bank=bank,
        accounts=accounts,
        fetch=fetch,
        state_file=state_file,
        tan=_tan(_table(data, "tan", required=False)),
        delivery=_delivery(_table(data, "delivery", required=False)),
        healthcheck=_healthcheck(_table(data, "healthcheck", required=False)),
    )


def _table(data: Mapping[str, Any], key: str, required: bool = True) -> Mapping[str, Any]:
    value = data.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"the configuration needs a [{key}] table")
    return value


def _string(data: Mapping[str, Any], key: str, where: str, default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}{key} must be a non-empty string")
    return value.strip()


def _optional_string(data: Mapping[str, Any], key: str, where: str) -> str | None:
    if data.get(key) is None:
        return None
    return _string(data, key, where)


def _env_name(data: Mapping[str, Any], key: str, where: str, default: str | None = None) -> str:
    value = _string(data, key, where, default=default)
    if not _ENV_NAME.match(value):
        raise ConfigError(f"{where}{key} must be the name of an environment variable")
    return value


def _int(data: Mapping[str, Any], key: str, where: str, default: int, minimum: int, maximum: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{where}{key} must be an integer from {minimum} to {maximum}")
    return value


def _https_url(data: Mapping[str, Any], key: str, where: str) -> str | None:
    url = _optional_string(data, key, where)
    if url is not None and not url.startswith("https://"):
        raise ConfigError(f"{where}{key} must use https://")
    return url


def _path(value: str, base_dir: Path) -> Path:
    """A path of the configuration; a relative one is relative to the configuration file."""
    path = Path(value)
    return path if path.is_absolute() else base_dir / path


def _date(data: Mapping[str, Any], key: str, where: str) -> datetime.date | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, datetime.datetime) or not isinstance(value, datetime.date):
        raise ConfigError(f"{where}{key} must be a date (YYYY-MM-DD, unquoted)")
    return value


def _bank(data: Mapping[str, Any], base_dir: Path) -> BankConfig:
    where = "bank."
    blz = _string(data, "blz", where)
    if not _BLZ.match(blz):
        raise ConfigError("bank.blz must be the 8-digit bank code")
    url = _string(data, "url", where)
    if not url.startswith("https://"):
        raise ConfigError("bank.url must be the bank's FinTS URL (https://)")
    bic = _optional_string(data, "bic", where)
    if bic is not None and not _BIC.match(bic.upper()):
        raise ConfigError("bank.bic is not a valid BIC")
    pin_file = _optional_string(data, "pin_file", where)
    return BankConfig(
        blz=blz,
        url=url,
        user_id=_string(data, "user_id", where),
        customer_id=_optional_string(data, "customer_id", where),
        bic=bic.upper() if bic else None,
        pin_env=_env_name(data, "pin_env", where, default="FINTS_PIN"),
        pin_file=_path(pin_file, base_dir) if pin_file else None,
    )


def _fetch(data: Mapping[str, Any]) -> FetchConfig:
    fmt = data.get("format", "auto")
    if fmt not in ("auto", "camt", "mt940"):
        raise ConfigError("fetch.format must be one of auto, camt, mt940")
    overlap = data.get("overlap_days", DEFAULT_OVERLAP_DAYS)
    if isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 0:
        raise ConfigError("fetch.overlap_days must be a non-negative integer")
    return FetchConfig(format=fmt, overlap_days=overlap, start_date=_date(data, "start_date", "fetch."))


def _tan(data: Mapping[str, Any]) -> TanConfig:
    return TanConfig(
        wait_minutes=_int(data, "wait_minutes", "tan.", DEFAULT_TAN_WAIT_MINUTES, 0, 60),
        poll_seconds=_int(data, "poll_seconds", "tan.", DEFAULT_TAN_POLL_SECONDS, 1, 300),
    )


def _delivery(data: Mapping[str, Any]) -> DeliveryConfig:
    return DeliveryConfig(
        attempts=_int(data, "attempts", "delivery.", DEFAULT_DELIVERY_ATTEMPTS, 1, 10),
        backoff_seconds=_int(data, "backoff_seconds", "delivery.", DEFAULT_DELIVERY_BACKOFF_SECONDS, 0, 600),
        timeout_seconds=_int(data, "timeout_seconds", "delivery.", DEFAULT_DELIVERY_TIMEOUT_SECONDS, 1, 600),
    )


def _healthcheck(data: Mapping[str, Any]) -> HealthcheckConfig:
    return HealthcheckConfig(
        success_url=_https_url(data, "success_url", "healthcheck."),
        failure_url=_https_url(data, "failure_url", "healthcheck."),
    )


def _account(data: Any, index: int, base_dir: Path) -> AccountConfig:
    where = f"account #{index}: "
    if not isinstance(data, Mapping):
        raise ConfigError(f"{where}must be a table")
    iban = normalise_iban(_string(data, "iban", where))
    if not iban_is_valid(iban):
        raise ConfigError(f"{where}iban is not a valid IBAN")
    currency = _string(data, "currency", where, default="EUR").upper()
    if not _CURRENCY.match(currency):
        raise ConfigError(f"{where}currency must be an ISO 4217 code")
    target = data.get("target")
    if not isinstance(target, Mapping):
        raise ConfigError(f"{where}needs a target (url + token_env or token_file, or folder)")
    return AccountConfig(
        iban=iban,
        target=_target(target, where, base_dir),
        currency=currency,
        start_date=_date(data, "start_date", where),
    )


def _target(data: Mapping[str, Any], where: str, base_dir: Path) -> TargetConfig:
    where = f"{where}target."
    has_url, has_folder = data.get("url") is not None, data.get("folder") is not None
    if has_url == has_folder:
        raise ConfigError(f"{where}needs exactly one of url or folder")
    if has_url:
        url = _string(data, "url", where)
        if not url.startswith("https://"):
            raise ConfigError(f"{where}url must use https://")
        if (data.get("token_env") is None) == (data.get("token_file") is None):
            raise ConfigError(f"{where}needs exactly one of token_env or token_file")
        if data.get("token_file") is not None:
            return TargetConfig(url=url, token_file=_path(_string(data, "token_file", where), base_dir))
        return TargetConfig(url=url, token_env=_env_name(data, "token_env", where))
    return TargetConfig(folder=Path(_string(data, "folder", where)))


def normalise_iban(iban: str) -> str:
    return "".join(iban.split()).upper()


def mask_iban(iban: str) -> str:
    """The IBAN for messages and logs: country, check digits and the last four characters."""
    return f"{iban[:4]}…{iban[-4:]}"


def iban_is_valid(iban: str) -> bool:
    """ISO 13616 check (format and mod-97 checksum)."""
    if not re.match(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$", iban):
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged)
    return int(digits) % 97 == 1


def read_pin(bank: BankConfig, environ: Mapping[str, str] | None = None) -> str:
    """Read the PIN from the secret file or the environment variable the config names.

    Error messages name where the PIN was looked for, never its value.
    """
    if bank.pin_file is not None:
        try:
            pin = bank.pin_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read the PIN file {bank.pin_file}: {exc.strerror}") from None
        if not pin:
            raise ConfigError(f"the PIN file {bank.pin_file} is empty")
        return pin
    env = os.environ if environ is None else environ
    pin = env.get(bank.pin_env, "").strip()
    if not pin:
        raise ConfigError(f"the PIN is not set: environment variable {bank.pin_env} is empty")
    return pin


def read_token(target: TargetConfig, environ: Mapping[str, str] | None = None) -> str:
    """The bearer token of an HTTPS target, from the secret file or the environment variable
    the config names. Error messages name where it was looked for, never its value."""
    if target.token_file is not None:
        try:
            token = target.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read the token file {target.token_file}: {exc.strerror}") from None
        if not token:
            raise ConfigError(f"the token file {target.token_file} is empty")
        return token
    if target.token_env is None:
        raise ConfigError("the target has neither token_env nor token_file")
    env = os.environ if environ is None else environ
    token = env.get(target.token_env, "").strip()
    if not token:
        raise ConfigError(f"the target token is not set: environment variable {target.token_env} is empty")
    return token
