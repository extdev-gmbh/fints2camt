import datetime
import json
import stat

import pytest

from fints2camt.config import AccountConfig, FetchConfig, TargetConfig
from fints2camt.errors import AlreadyRunningError, StateError
from fints2camt.state import MAX_FAILED_LOGINS, State, fetch_window, load_state, save_state, state_lock

from conftest import OWN_IBAN

TODAY = datetime.date(2026, 10, 25)
ACCOUNT = AccountConfig(iban=OWN_IBAN, target=TargetConfig(folder=None))


def test_first_run_defaults_to_90_days() -> None:
    window = fetch_window(State(), ACCOUNT, FetchConfig(), TODAY)
    assert (window.start, window.end) == (datetime.date(2026, 7, 27), TODAY)


def test_first_run_uses_configured_start_dates() -> None:
    fetch = FetchConfig(start_date=datetime.date(2026, 1, 1))
    assert fetch_window(State(), ACCOUNT, fetch, TODAY).start == datetime.date(2026, 1, 1)
    account = AccountConfig(iban=OWN_IBAN, target=ACCOUNT.target, start_date=datetime.date(2026, 6, 1))
    assert fetch_window(State(), account, fetch, TODAY).start == datetime.date(2026, 6, 1)


def test_later_runs_overlap_the_last_delivery() -> None:
    state = State(delivered_through={OWN_IBAN: datetime.date(2026, 10, 20)})
    assert fetch_window(state, ACCOUNT, FetchConfig(), TODAY).start == datetime.date(2026, 10, 17)
    assert fetch_window(state, ACCOUNT, FetchConfig(overlap_days=0), TODAY).start == datetime.date(2026, 10, 20)
    # a delivery made today still fetches the overlap, never a window in the future
    state.delivered_through[OWN_IBAN] = TODAY + datetime.timedelta(days=5)
    window = fetch_window(state, ACCOUNT, FetchConfig(overlap_days=0), TODAY)
    assert window.start == window.end == TODAY


def test_mark_delivered_never_moves_backwards() -> None:
    state = State()
    state.mark_delivered(OWN_IBAN, datetime.date(2026, 10, 20))
    state.mark_delivered(OWN_IBAN, datetime.date(2026, 10, 18))
    assert state.delivered_through[OWN_IBAN] == datetime.date(2026, 10, 20)
    state.mark_delivered(OWN_IBAN, datetime.date(2026, 10, 25))
    assert state.delivered_through[OWN_IBAN] == datetime.date(2026, 10, 25)


def test_missing_state_file_is_a_first_run(tmp_path) -> None:
    assert load_state(tmp_path / "state.json") == State()


def test_round_trip_is_private_and_keeps_unknown_keys(tmp_path) -> None:
    path = tmp_path / "sub" / "state.json"
    state = State(
        delivered_through={OWN_IBAN: datetime.date(2026, 10, 24)},
        fints_client_data=b"\x00opaque\xff",
        extra={"approval": {"pending": True}},
    )
    save_state(path, state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_state(path) == state
    assert json.loads(path.read_text())["accounts"][OWN_IBAN] == {"delivered_through": "2026-10-24"}
    assert [p.name for p in path.parent.iterdir()] == ["state.json"]


@pytest.mark.parametrize("content", ["{", '{"version": 99}', '{"version": 1, "accounts": {"X": {"delivered_through": "no"}}}'])
def test_corrupt_state_raises(tmp_path, content: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(content)
    with pytest.raises(StateError):
        load_state(path)


def test_approval_and_pin_marks_round_trip_and_are_cleared_together(tmp_path) -> None:
    path = tmp_path / "state.json"
    since = datetime.datetime(2026, 10, 25, 6, 0, 12, tzinfo=datetime.timezone.utc)
    state = State(fints_client_data=b"blob", approval_needed_since=since, pin_rejected_since=since)
    save_state(path, state)
    assert load_state(path) == state
    assert json.loads(path.read_text())["fints"]["approval_needed_since"] == "2026-10-25T06:00:12+00:00"
    state.bank_access_cleared()
    save_state(path, state)
    loaded = load_state(path)
    assert (loaded.approval_needed_since, loaded.pin_rejected_since) == (None, None)


def test_failed_logins_round_trip_and_are_cleared_by_approve(tmp_path) -> None:
    path = tmp_path / "state.json"
    when = datetime.datetime(2026, 10, 25, 6, 0, tzinfo=datetime.timezone.utc)
    state = State(fints_client_data=b"blob")
    assert load_state_of(path, state).failed_logins == 0
    state.login_failed(when)
    state.login_failed(when)
    assert state.logins_exhausted and MAX_FAILED_LOGINS == 2
    assert load_state_of(path, state) == state
    state.bank_access_cleared()
    assert (state.failed_logins, state.last_failed_login, state.logins_exhausted) == (0, None, False)


def test_a_state_of_the_previous_layout_has_no_failed_logins(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "accounts": {}, "fints": {"client_data": None}}))
    assert load_state(path).failed_logins == 0


def load_state_of(path, state: State) -> State:
    save_state(path, state)
    return load_state(path)


def test_state_lock_is_exclusive_and_released(tmp_path) -> None:
    path = tmp_path / "sub" / "state.json"
    with state_lock(path):
        assert stat.S_IMODE((tmp_path / "sub" / "state.json.lock").stat().st_mode) == 0o600
        with pytest.raises(AlreadyRunningError, match="another fints2camt is running"):
            with state_lock(path):
                pass
    with state_lock(path):
        pass
