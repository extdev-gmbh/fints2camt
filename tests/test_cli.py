import pytest

from fints2camt import cli, commands

CONFIG = """
state_file = "state.json"

[bank]
blz = "37040044"
url = "https://fints.example-bank.invalid/fints"
user_id = "synthetic-user"

[healthcheck]
success_url = "https://kuma.invalid/push/synthetic?status=up"
failure_url = "https://kuma.invalid/push/synthetic?status=down"

[[account]]
iban = "DE89370400440532013000"
target = { folder = "out" }
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


@pytest.fixture
def pings(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(cli.healthcheck, "ping", lambda config, success: calls.append(success))
    return calls


@pytest.mark.parametrize(
    "outcome",
    [
        commands.Outcome(commands.EXIT_OK, "ok – 1/1 accounts delivered, 3 entries"),
        commands.Outcome(commands.EXIT_ACTION_NEEDED, "approval needed – run `fints2camt approve`"),
        commands.Outcome(commands.EXIT_PIN, "stopped – the bank rejected the PIN"),
    ],
)
def test_run_prints_one_summary_line_and_pings(config_file, pings, monkeypatch, capsys, outcome) -> None:
    monkeypatch.setattr(cli.commands, "run", lambda config: outcome)
    assert cli.main(["--config", str(config_file), "run"]) == outcome.exit_code
    assert capsys.readouterr().out == f"fints2camt run: {outcome.summary}\n"
    assert pings == [outcome.ok]


def test_config_from_the_environment_variable(config_file, pings, monkeypatch, capsys) -> None:
    monkeypatch.setenv(cli.CONFIG_ENV, str(config_file))
    seen = []
    monkeypatch.setattr(cli.commands, "check", lambda config: seen.append(config) or 0)
    assert cli.main(["check"]) == 0
    assert seen[0].bank.blz == "37040044"
    assert pings == []  # only `run` pings


def test_invalid_config_exits_2(tmp_path, pings, capsys) -> None:
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "run"]) == commands.EXIT_CONFIG
    assert "configuration error" in capsys.readouterr().out
    assert pings == []


def test_unexpected_error_still_ends_with_a_summary_and_a_failure_ping(config_file, pings, monkeypatch, capsys) -> None:
    def boom(config):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(cli.commands, "run", boom)
    assert cli.main(["--config", str(config_file), "run"]) == commands.EXIT_FAILED
    assert capsys.readouterr().out == "fints2camt run: failed – unexpected RuntimeError\n"
    assert pings == [False]


def test_unknown_command_is_a_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["fetch"])
    assert excinfo.value.code == 2
