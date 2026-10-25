import datetime
import tomllib
from pathlib import Path

import pytest

from fints2camt.config import (
    DeliveryConfig,
    HealthcheckConfig,
    TanConfig,
    iban_is_valid,
    load_config,
    parse_config,
    read_pin,
    read_token,
)
from fints2camt.errors import ConfigError

EXAMPLE = """
state_file = "state/state.json"

[bank]
blz = "37040044"
url = "https://fints.example-bank.invalid/fints"
user_id = "synthetic-user"
bic = "cobadeffxxx"

[fetch]
overlap_days = 2
start_date = 2026-01-01

[[account]]
iban = "DE89 3704 0044 0532 0130 00"
target = { url = "https://accounting.example.invalid/camt", token_env = "TARGET_TOKEN" }

[[account]]
iban = "de02120300000000202051"
start_date = 2026-06-01
target = { folder = "/data/camt" }
"""


def parse(text: str = EXAMPLE):
    return parse_config(tomllib.loads(text), base_dir=Path("/etc/fints2camt"))


def test_example_config() -> None:
    config = parse()
    assert config.bank.blz == "37040044"
    assert config.bank.bic == "COBADEFFXXX"
    assert config.bank.pin_env == "FINTS_PIN"
    assert config.fetch.format == "auto"
    assert config.fetch.overlap_days == 2
    assert config.fetch.start_date == datetime.date(2026, 1, 1)
    assert config.state_file == Path("/etc/fints2camt/state/state.json")
    first, second = config.accounts
    assert first.iban == "DE89370400440532013000"
    assert first.target.url == "https://accounting.example.invalid/camt"
    assert first.target.token_env == "TARGET_TOKEN"
    assert second.iban == "DE02120300000000202051"
    assert second.target.folder == Path("/data/camt")
    assert second.start_date == datetime.date(2026, 6, 1)


def test_load_config_from_file(tmp_path) -> None:
    path = tmp_path / "fints2camt.toml"
    path.write_text(EXAMPLE)
    assert load_config(path).state_file == tmp_path / "state" / "state.json"
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")
    path.write_text("[bank")
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('blz = "37040044"', 'blz = "3704"'),
        ('url = "https://fints.example-bank.invalid/fints"', 'url = "http://fints.example-bank.invalid"'),
        ('iban = "DE89 3704 0044 0532 0130 00"', 'iban = "DE00 3704 0044 0532 0130 00"'),
        ('target = { folder = "/data/camt" }', 'target = { folder = "/x", url = "https://y.invalid" }'),
        ('token_env = "TARGET_TOKEN"', 'token_env = "not a name"'),
        ('url = "https://accounting.example.invalid/camt"', 'url = "http://accounting.example.invalid/camt"'),
        ("overlap_days = 2", "overlap_days = -1"),
        ("start_date = 2026-01-01", 'start_date = "2026-01-01"'),
        ('iban = "de02120300000000202051"', 'iban = "DE89370400440532013000"'),
        ("[fetch]", '[fetch]\nformat = "pdf"'),
        ("[fetch]", "[tan]\nwait_minutes = 61\n[fetch]"),
        ("[fetch]", "[tan]\npoll_seconds = 0\n[fetch]"),
        ("[fetch]", '[tan]\nwait_minutes = "5"\n[fetch]'),
        ("[fetch]", "[delivery]\nattempts = 0\n[fetch]"),
        ("[fetch]", "[delivery]\nbackoff_seconds = true\n[fetch]"),
        ("[fetch]", '[healthcheck]\nsuccess_url = "http://kuma.invalid/push"\n[fetch]'),
    ],
)
def test_invalid_configs_are_rejected(old: str, new: str) -> None:
    assert old in EXAMPLE
    with pytest.raises(ConfigError):
        parse(EXAMPLE.replace(old, new, 1))


def test_config_without_accounts_is_rejected() -> None:
    with pytest.raises(ConfigError):
        parse(EXAMPLE.split("[[account]]")[0])


def test_iban_check() -> None:
    assert iban_is_valid("DE89370400440532013000")
    assert not iban_is_valid("DE89370400440532013001")
    assert not iban_is_valid("XX")


def test_pin_from_env_and_file(tmp_path) -> None:
    config = parse()
    assert read_pin(config.bank, {"FINTS_PIN": " 12345 "}) == "12345"
    with pytest.raises(ConfigError) as excinfo:
        read_pin(config.bank, {})
    assert "FINTS_PIN" in str(excinfo.value)

    pin_file = tmp_path / "pin"
    pin_file.write_text("67890\n")
    with_file = parse(EXAMPLE.replace('bic = "cobadeffxxx"', f'bic = "cobadeffxxx"\npin_file = "{pin_file}"'))
    assert read_pin(with_file.bank, {"FINTS_PIN": "ignored"}) == "67890"
    pin_file.write_text("")
    with pytest.raises(ConfigError):
        read_pin(with_file.bank, {})


def test_pin_never_appears_in_errors(tmp_path) -> None:
    pin_file = tmp_path / "missing-pin"
    config = parse(EXAMPLE.replace('bic = "cobadeffxxx"', f'pin_file = "{pin_file}"'))
    with pytest.raises(ConfigError) as excinfo:
        read_pin(config.bank, {"FINTS_PIN": "secret-pin-1234"})
    assert "secret-pin-1234" not in str(excinfo.value)


def test_tan_delivery_and_healthcheck_defaults() -> None:
    config = parse()
    assert config.tan == TanConfig(wait_minutes=5, poll_seconds=5)
    assert config.delivery == DeliveryConfig(attempts=4, backoff_seconds=5, timeout_seconds=60)
    assert config.healthcheck == HealthcheckConfig()


def test_tan_delivery_and_healthcheck_settings() -> None:
    config = parse(EXAMPLE.replace("[fetch]", """[tan]
wait_minutes = 0
poll_seconds = 10

[delivery]
attempts = 1
backoff_seconds = 0
timeout_seconds = 5

[healthcheck]
failure_url = "https://kuma.invalid/api/push/synthetic?status=down"

[fetch]"""))
    assert config.tan == TanConfig(wait_minutes=0, poll_seconds=10)
    assert config.delivery == DeliveryConfig(attempts=1, backoff_seconds=0, timeout_seconds=5)
    assert config.healthcheck.success_url is None
    assert config.healthcheck.failure_url == "https://kuma.invalid/api/push/synthetic?status=down"


def test_token_from_env() -> None:
    target = parse().accounts[0].target
    assert read_token(target, {"TARGET_TOKEN": " synthetic-token "}) == "synthetic-token"
    with pytest.raises(ConfigError, match="TARGET_TOKEN"):
        read_token(target, {"TARGET_TOKEN": ""})


def test_relative_secret_and_state_paths_are_relative_to_the_configuration(tmp_path) -> None:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "pin").write_text("67890\n")
    (tmp_path / "secrets" / "token").write_text(" file-token \n")
    text = EXAMPLE.replace('bic = "cobadeffxxx"', 'pin_file = "secrets/pin"').replace(
        'token_env = "TARGET_TOKEN"', 'token_file = "secrets/token"'
    )
    path = tmp_path / "config.toml"
    path.write_text(text)
    config = load_config(path)
    assert config.state_file == tmp_path / "state" / "state.json"
    assert config.bank.pin_file == tmp_path / "secrets" / "pin"
    assert read_pin(config.bank, {}) == "67890"
    target = config.accounts[0].target
    assert (target.token_env, target.token_file) == (None, tmp_path / "secrets" / "token")
    assert read_token(target, {"TARGET_TOKEN": "ignored"}) == "file-token"


def test_token_file_errors_name_the_file_never_the_token(tmp_path) -> None:
    token_file = tmp_path / "token"
    config = parse(EXAMPLE.replace('token_env = "TARGET_TOKEN"', f'token_file = "{token_file}"'))
    target = config.accounts[0].target
    with pytest.raises(ConfigError, match="cannot read the token file"):
        read_token(target, {})
    token_file.write_text("\n")
    with pytest.raises(ConfigError, match="is empty"):
        read_token(target, {})


@pytest.mark.parametrize(
    "target",
    [
        'target = { url = "https://accounting.example.invalid/camt" }',
        'target = { url = "https://accounting.example.invalid/camt", token_env = "T", token_file = "/t" }',
    ],
)
def test_an_https_target_needs_exactly_one_token_source(target: str) -> None:
    text = EXAMPLE.replace(
        'target = { url = "https://accounting.example.invalid/camt", token_env = "TARGET_TOKEN" }', target
    )
    with pytest.raises(ConfigError, match="exactly one of token_env or token_file"):
        parse(text)
