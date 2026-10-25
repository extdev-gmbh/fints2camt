"""setup / run / approve / check end to end: the real FinTSSource, conversion and delivery,
against a mocked python-fints client and a mocked HTTP target (synthetic data only)."""

import datetime
import fcntl
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fints.exceptions import FinTSClientError, FinTSClientPINError, FinTSConnectionError, FinTSDialogInitError

from fints2camt import commands
from fints2camt.config import Config, parse_config
from fints2camt.deliver import deliver as deliver_statement
from fints2camt.fints_source import FinTSSource
from fints2camt.state import State, load_state, save_state

from conftest import OWN_IBAN, fixture_text
from fake_fints import OTHER_IBAN, FakeClient, client_factory, tan_challenge

NOW = datetime.datetime(2026, 10, 25, 6, 0, tzinfo=datetime.timezone.utc)
TODAY = NOW.astimezone().date()
ENV = {"FINTS_PIN": "1234", "FINTS_PRODUCT_ID": "SYNTHETICPRODUCTID", "TARGET_TOKEN": "synthetic-token"}
MT940 = [SimpleNamespace(statement_booked=fixture_text("mt940_synthetic.sta").encode("iso-8859-1"))]


def make_config(tmp_path: Path, wait_minutes: int = 1) -> Config:
    text = f"""
state_file = "state.json"

[bank]
blz = "37040044"
url = "https://fints.example-bank.invalid/fints"
user_id = "synthetic-user"

[fetch]
start_date = 2026-10-21

[tan]
wait_minutes = {wait_minutes}
poll_seconds = 5

[delivery]
attempts = 3
backoff_seconds = 1

[[account]]
iban = "{OWN_IBAN}"
target = {{ url = "https://accounting.example.invalid/camt", token_env = "TARGET_TOKEN" }}

[[account]]
iban = "{OTHER_IBAN}"
target = {{ folder = "{tmp_path / 'out'}" }}
"""
    return parse_config(tomllib.loads(text), base_dir=tmp_path)


def set_up(config: Config, **fields) -> None:
    save_state(config.state_file, State(fints_client_data=b"setup-blob", **fields))


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Bank:
    """Builds FinTSSource objects on mocked clients; counts how often the bank is contacted."""

    def __init__(self, **overrides) -> None:
        self.overrides = {"camt_supported": False, "kaz_segments": MT940, "accounts": [OWN_IBAN, OTHER_IBAN]}
        self.overrides.update(overrides)
        self.clients: list[FakeClient] = []
        self.client_data_in: list[bytes | None] = []

    def factory(self, bank, pin, product_id, client_data, approver) -> FinTSSource:
        self.client_data_in.append(client_data)
        return FinTSSource(
            bank, pin, product_id, client_data=client_data,
            client_factory=client_factory(self.clients, **self.overrides), approver=approver,
        )

    @property
    def dialogs(self) -> int:
        return sum(client.entered for client in self.clients)


class Target:
    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(request.content)
        return httpx.Response(self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0])


def run(config: Config, bank: Bank, target: Target | None = None, environ=ENV, time: FakeTime | None = None):
    target = target or Target(200)
    time = time or FakeTime()

    def deliver(result, account):
        return deliver_statement(
            result, account.target, config.delivery, environ=environ,
            transport=httpx.MockTransport(target), sleep=lambda _: None,
        )

    return commands.run(
        config, environ=environ, now=NOW, source_factory=bank.factory, deliver=deliver,
        sleep=time.sleep, clock=time.clock,
    )


def test_run_delivers_every_account_and_advances_the_state(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank, target = Bank(), Target(200)
    outcome = run(config, bank, target)
    assert outcome == commands.Outcome(0, "ok – 2/2 accounts delivered, 10 entries")
    assert bank.client_data_in == [b"setup-blob"]
    assert len(target.bodies) == 1 and target.bodies[0].startswith(b"<?xml")
    assert (tmp_path / "out" / f"{OTHER_IBAN}_2026-10-21_{TODAY}.camt053.xml").exists()
    state = load_state(config.state_file)
    assert state.delivered_through == {OWN_IBAN: TODAY, OTHER_IBAN: TODAY}
    assert state.fints_client_data == b"client-blob"


def test_4xx_leaves_only_that_account_behind(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    target = Target(400)
    outcome = run(config, Bank(), target)
    assert outcome.exit_code == commands.EXIT_FAILED
    assert outcome.summary.startswith("failed – 1/2 accounts delivered")
    assert "DE89…3000: the target answered HTTP 400; not retrying" in outcome.summary
    assert len(target.bodies) == 1
    assert load_state(config.state_file).delivered_through == {OTHER_IBAN: TODAY}


def test_5xx_is_retried_and_then_delivered(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    target = Target(503, 502, 200)
    assert run(config, Bank(), target).exit_code == commands.EXIT_OK
    assert len(target.bodies) == 3
    assert OWN_IBAN in load_state(config.state_file).delivered_through


def test_persistent_5xx_does_not_advance_the_state(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config, delivered_through={OWN_IBAN: datetime.date(2026, 10, 22)})
    target = Target(503)
    outcome = run(config, Bank(), target)
    assert outcome.exit_code == commands.EXIT_FAILED
    assert "HTTP 503 after 3 attempt(s)" in outcome.summary
    assert len(target.bodies) == 3
    assert load_state(config.state_file).delivered_through[OWN_IBAN] == datetime.date(2026, 10, 22)


def test_pending_approval_that_arrives_in_time(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank(init_challenge=tan_challenge(), tan_answers=[tan_challenge(), tan_challenge(), "dialog open"])
    time = FakeTime()
    outcome = run(config, bank, time=time)
    assert outcome.exit_code == commands.EXIT_OK
    assert len(bank.clients[0].tans_sent) == 3
    assert time.now == 15
    assert load_state(config.state_file).approval_needed_since is None


def test_approval_timeout_marks_the_state_and_starts_no_new_challenge(tmp_path) -> None:
    config = make_config(tmp_path, wait_minutes=1)
    set_up(config)
    bank = Bank(init_challenge=tan_challenge(), tan_answers=[tan_challenge() for _ in range(100)])
    target = Target(200)
    outcome = run(config, bank, target)
    assert outcome == commands.Outcome(commands.EXIT_ACTION_NEEDED, "approval needed – run `fints2camt approve`")
    assert len(bank.clients[0].tans_sent) == 12  # one minute of status queries, every 5 seconds
    assert not bank.clients[0].open
    assert target.bodies == [] and not (tmp_path / "out").exists()
    state = load_state(config.state_file)
    assert state.approval_needed_since == NOW
    assert state.delivered_through == {}

    # the following runs do not contact the bank at all
    for _ in range(3):
        outcome = run(config, bank, target)
        assert outcome.exit_code == commands.EXIT_ACTION_NEEDED
        assert outcome.summary.startswith("approval needed – run `fints2camt approve` (pending since 2026-10-25")
    assert bank.dialogs == 1 and len(bank.client_data_in) == 1


def assert_no_further_bank_contact(config: Config, bank: Bank) -> None:
    dialogs, sources = bank.dialogs, len(bank.client_data_in)
    for _ in range(3):
        outcome = run(config, bank)
        assert outcome.summary.startswith("approval needed – run `fints2camt approve` (pending since 2026-10-25")
    assert (bank.dialogs, len(bank.client_data_in)) == (dialogs, sources)


def test_bank_error_while_waiting_at_dialog_start_marks_the_state(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank(init_challenge=tan_challenge(), tan_answers=[tan_challenge(), FinTSClientError("No TAN status received.")])
    target = Target(200)
    outcome = run(config, bank, target)
    assert outcome == commands.Outcome(commands.EXIT_ACTION_NEEDED, "approval needed – run `fints2camt approve`")
    assert bank.dialogs == 1 and len(bank.clients[0].tans_sent) == 2
    assert not bank.clients[0].open
    assert target.bodies == []
    assert load_state(config.state_file).approval_needed_since == NOW
    assert_no_further_bank_contact(config, bank)


def test_bank_error_while_waiting_for_an_operation_stops_before_the_next_account(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank(kaz_challenge=tan_challenge(), tan_answers=[FinTSClientError("No TAN status received.")])
    target = Target(200)
    outcome = run(config, bank, target)
    assert outcome == commands.Outcome(commands.EXIT_ACTION_NEEDED, "approval needed – run `fints2camt approve`")
    client = bank.clients[0]
    # one challenge for the first account; the second account is not fetched (no second push)
    assert [request[0] for request in client.sent] == ["HKKAZ"]
    assert len(client.tans_sent) == 1
    assert target.bodies == [] and not (tmp_path / "out").exists()
    state = load_state(config.state_file)
    assert state.approval_needed_since == NOW
    assert state.delivered_through == {}
    assert_no_further_bank_contact(config, bank)


def test_pin_rejected_while_waiting_stays_a_pin_stop(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank(init_challenge=tan_challenge(), tan_answers=[FinTSClientPINError("PIN wrong?")])
    outcome = run(config, bank)
    assert outcome.exit_code == commands.EXIT_PIN
    state = load_state(config.state_file)
    assert state.pin_rejected_since == NOW
    assert state.approval_needed_since is None


def test_typed_tan_challenge_in_a_run_is_not_waited_for(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank(init_challenge=tan_challenge(decoupled=False))
    assert run(config, bank).exit_code == commands.EXIT_ACTION_NEEDED
    assert bank.clients[0].tans_sent == []
    assert load_state(config.state_file).approval_needed_since == NOW


def approve(config: Config, bank: Bank, answers: list[str] | None = None) -> tuple[int, list[str]]:
    output: list[str] = []
    answers = list(answers or [])
    time = FakeTime()
    code = commands.approve(
        config, environ=ENV, prompt=lambda _: answers.pop(0), out=output.append, now=NOW,
        source_factory=bank.factory, sleep=time.sleep, clock=time.clock,
    )
    return code, output


def test_approve_clears_the_mark_and_the_next_run_delivers(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config, approval_needed_since=NOW)
    bank = Bank(init_challenge=tan_challenge(text="Approve the login"), tan_answers=[tan_challenge(), "dialog open"])
    code, output = approve(config, bank)
    assert code == commands.EXIT_OK
    assert "The bank says: Approve the login" in output
    assert output[-1].startswith("approved")
    assert load_state(config.state_file).approval_needed_since is None
    assert run(config, Bank()).exit_code == commands.EXIT_OK


def test_approve_that_is_not_approved_keeps_the_mark(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config, approval_needed_since=NOW)
    bank = Bank(init_challenge=tan_challenge(), tan_answers=[tan_challenge() for _ in range(200)])
    code, output = approve(config, bank)
    assert code == commands.EXIT_ACTION_NEEDED
    assert output[-1].startswith("not approved")
    assert load_state(config.state_file).approval_needed_since == NOW


def test_approve_needs_setup_first(tmp_path) -> None:
    config = make_config(tmp_path)
    bank = Bank()
    assert approve(config, bank)[0] == commands.EXIT_ACTION_NEEDED
    assert bank.clients == []


def test_wrong_pin_stops_at_once_and_blocks_later_runs(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank(fail_pin=True)
    outcome = run(config, bank)
    assert outcome.exit_code == commands.EXIT_PIN
    assert "1234" not in outcome.summary
    assert bank.dialogs == 1
    assert load_state(config.state_file).pin_rejected_since == NOW

    outcome = run(config, bank)
    assert outcome.exit_code == commands.EXIT_PIN
    assert "fints2camt approve" in outcome.summary
    assert bank.dialogs == 1  # no second attempt with the rejected PIN

    # approve with the still wrong PIN: one more deliberate attempt, still blocked
    assert approve(config, bank)[0] == commands.EXIT_PIN
    assert bank.dialogs == 2
    # with the fixed PIN the access is cleared
    assert approve(config, Bank())[0] == commands.EXIT_OK
    assert load_state(config.state_file).pin_rejected_since is None


def test_refused_dialog_starts_are_counted_and_stop_runs_after_two(tmp_path) -> None:
    # A bank answering a wrong PIN with a code python-fints does not recognise as such.
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank(fail_init=FinTSDialogInitError("Dialog initialization failed"))
    outcome = run(config, bank)
    assert outcome.exit_code == commands.EXIT_FAILED
    assert "refused start 1 of 2" in outcome.summary and "1234" not in outcome.summary
    state = load_state(config.state_file)
    assert (state.failed_logins, state.last_failed_login, state.pin_rejected_since) == (1, NOW, None)
    assert state.fints_client_data == b"setup-blob"

    outcome = run(config, bank)
    assert outcome.exit_code == commands.EXIT_PIN
    assert "refused the start of the dialog 2 times in a row" in outcome.summary
    assert bank.dialogs == 2

    # from now on the bank is not contacted, until approve succeeded
    for _ in range(3):
        assert run(config, bank).exit_code == commands.EXIT_PIN
    assert bank.dialogs == 2
    assert approve(config, Bank())[0] == commands.EXIT_OK
    assert load_state(config.state_file).failed_logins == 0
    assert run(config, Bank()).exit_code == commands.EXIT_OK


def test_a_dialog_that_starts_resets_the_count(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    assert run(config, Bank(fail_init=FinTSDialogInitError("refused"))).exit_code == commands.EXIT_FAILED
    assert run(config, Bank()).exit_code == commands.EXIT_OK
    assert load_state(config.state_file).failed_logins == 0
    assert run(config, Bank(fail_init=FinTSDialogInitError("refused"))).exit_code == commands.EXIT_FAILED
    assert load_state(config.state_file).failed_logins == 1


def test_connection_errors_are_not_counted(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank(fail_init=FinTSConnectionError("Could not connect to FinTS endpoint"))
    for _ in range(3):
        assert run(config, bank).exit_code == commands.EXIT_FAILED
    assert bank.dialogs == 3
    assert load_state(config.state_file).failed_logins == 0


def test_approve_counts_a_refused_dialog_start_too(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config, failed_logins=1)
    code, output = approve(config, Bank(fail_init=FinTSDialogInitError("refused")))
    assert code == commands.EXIT_PIN
    assert "check the PIN" in output[-1]
    assert load_state(config.state_file).failed_logins == 2


def hold_lock(config: Config) -> int:
    """Take the state lock as a concurrent fints2camt process would."""
    fd = os.open(f"{config.state_file}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_a_second_run_setup_or_approve_exits_while_another_holds_the_lock(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config, approval_needed_since=NOW)
    before = config.state_file.read_bytes()
    bank = Bank()
    fd = hold_lock(config)
    try:
        outcome = run(config, bank)
        assert outcome.exit_code == commands.EXIT_FAILED
        assert outcome.summary.startswith("failed – another fints2camt is running")
        assert approve(config, bank) == (commands.EXIT_FAILED, [outcome.summary])
        output: list[str] = []
        assert commands.setup(config, environ=ENV, prompt=input, out=output.append, source_factory=bank.factory) == commands.EXIT_FAILED
        assert output[0].startswith("failed – another fints2camt is running")
        assert bank.clients == []
        assert config.state_file.read_bytes() == before
    finally:
        os.close(fd)
    # released: the next one runs
    assert approve(config, Bank(init_challenge=tan_challenge(), tan_answers=["dialog open"]))[0] == commands.EXIT_OK


def test_run_before_setup_does_not_contact_the_bank(tmp_path) -> None:
    config = make_config(tmp_path)
    bank = Bank()
    assert run(config, bank).exit_code == commands.EXIT_ACTION_NEEDED
    assert bank.clients == []


@pytest.mark.parametrize("missing", ["FINTS_PIN", "FINTS_PRODUCT_ID", "TARGET_TOKEN"])
def test_missing_secret_is_a_configuration_error_before_the_bank(tmp_path, missing: str) -> None:
    config = make_config(tmp_path)
    set_up(config)
    bank = Bank()
    environ = {key: value for key, value in ENV.items() if key != missing}
    outcome = run(config, bank, environ=environ)
    assert outcome.exit_code == commands.EXIT_CONFIG
    assert missing in outcome.summary
    assert bank.clients == []


def test_setup_prefers_decoupled_and_writes_the_state(tmp_path) -> None:
    config = make_config(tmp_path)
    bank = Bank(init_challenge=tan_challenge(), tan_answers=["dialog open"])
    output: list[str] = []
    time = FakeTime()
    code = commands.setup(
        config, environ=ENV, prompt=lambda _: "", out=output.append, now=NOW,
        source_factory=bank.factory, sleep=time.sleep, clock=time.clock,
    )
    assert code == commands.EXIT_OK
    assert bank.clients[0].selected_security_function == "921"
    assert "TAN method: pushTAN 2.0" in output
    state = load_state(config.state_file)
    assert state.fints_client_data == b"client-blob"
    assert state.approval_needed_since is None


def test_setup_warns_about_accounts_the_bank_access_does_not_see(tmp_path) -> None:
    config = make_config(tmp_path)
    bank = Bank(accounts=[OWN_IBAN])
    output: list[str] = []
    code = commands.setup(config, environ=ENV, prompt=lambda _: "1", out=output.append, now=NOW, source_factory=bank.factory)
    assert code == commands.EXIT_FAILED
    assert "warning: the bank access does not see the configured account DE02…2051" in output


def test_check_reports_without_opening_a_dialog(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(405)

    output: list[str] = []
    code = commands.check(config, environ=ENV, out=output.append, transport=httpx.MockTransport(handler))
    assert code == commands.EXIT_OK, output
    assert output[-1] == "all checks passed"
    assert [r.method for r in requests] == ["HEAD", "HEAD"]
    assert all("Authorization" not in r.headers and r.content == b"" for r in requests)
    assert not any("1234" in line or "synthetic-token" in line for line in output)


def test_check_fails_on_missing_secrets_unreachable_hosts_and_a_blocked_state(tmp_path) -> None:
    config = make_config(tmp_path)
    set_up(config, approval_needed_since=NOW)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable")

    output: list[str] = []
    code = commands.check(config, environ={"FINTS_PRODUCT_ID": "X"}, out=output.append, transport=httpx.MockTransport(handler))
    assert code == commands.EXIT_FAILED
    failures = [line for line in output if line.startswith("FAIL")]
    assert len(failures) == 5  # PIN, token, state, bank, target
    assert any("approval needed" in line for line in failures)
    assert output[-1] == "5 check(s) failed"
