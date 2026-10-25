"""The four commands: ``setup``, ``run``, ``approve`` (interactive only these two and setup)
and ``check``.

One ``run`` = fetch → convert → deliver → record → exit. Per account, the state advances
only after the target accepted the statement. The bank is not contacted at all while the
state records a pending approval (no new push notification on every timer run), a
rejected PIN or ``MAX_FAILED_LOGINS`` refused dialog starts in a row (no lock-out by
repeated attempts); ``approve`` or ``setup`` clears them. ``run``, ``setup`` and ``approve``
hold the lock of the state file: a second one started meanwhile exits at once.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import AccountConfig, BankConfig, Config, mask_iban, read_pin, read_token
from .deliver import deliver as deliver_statement
from .errors import (
    AlreadyRunningError,
    ApprovalPendingError,
    ConfigError,
    Fints2CamtError,
    LoginFailedError,
    StateError,
    TanRequiredError,
    WrongPinError,
)
from .fetch import FetchResult, fetch_statement
from .fints_source import FinTSSource, product_id_from_env
from .state import MAX_FAILED_LOGINS, State, fetch_window, load_state, save_state, state_lock
from .tan import (
    APPROVE_HINT,
    Approver,
    DecoupledWaiter,
    InteractiveApprover,
    Output,
    Prompt,
    choose_tan_mechanism,
    choose_tan_medium,
)

log = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1  # an account was not delivered, or another error
EXIT_CONFIG = 2  # configuration, secret or environment missing or invalid
EXIT_ACTION_NEEDED = 3  # the operator has to run `approve` (or `setup`)
EXIT_PIN = 4  # the bank rejected the PIN or locked the access: fix it, then `approve`

SourceFactory = Callable[[BankConfig, str, str, bytes | None, Approver], FinTSSource]
Deliver = Callable[[FetchResult, AccountConfig], str]


def default_source_factory(
    bank: BankConfig, pin: str, product_id: str, client_data: bytes | None, approver: Approver
) -> FinTSSource:
    return FinTSSource(bank, pin, product_id, client_data=client_data, approver=approver)


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    summary: str

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


# --- run -------------------------------------------------------------------------------


def run(
    config: Config,
    *,
    environ: Mapping[str, str] | None = None,
    now: datetime.datetime | None = None,
    source_factory: SourceFactory = default_source_factory,
    deliver: Deliver | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Outcome:
    """The scheduled run. Never prompts; returns the exit code and a one-line summary."""
    env = os.environ if environ is None else environ
    now = now or _now()
    today = now.astimezone().date()

    def deliver_to_target(result: FetchResult, account: AccountConfig) -> str:
        return deliver_statement(result, account.target, config.delivery, environ=env, sleep=sleep)

    deliver_one = deliver or deliver_to_target
    try:
        with state_lock(config.state_file):
            return _run(config, env, now, today, deliver_one, source_factory, sleep, clock)
    except AlreadyRunningError as exc:
        return Outcome(EXIT_FAILED, f"failed – {exc}")


def _run(
    config: Config,
    env: Mapping[str, str],
    now: datetime.datetime,
    today: datetime.date,
    deliver_one: Deliver,
    source_factory: SourceFactory,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> Outcome:
    state = load_state(config.state_file)
    blocked = _blocked(state)
    if blocked is not None:
        return blocked
    if state.fints_client_data is None:
        return Outcome(EXIT_ACTION_NEEDED, "not set up – run `fints2camt setup` first")
    try:
        pin = read_pin(config.bank, env)
        product_id = product_id_from_env(env)
        for account in config.accounts:
            if account.target.url is not None:
                read_token(account.target, env)
    except ConfigError as exc:
        return Outcome(EXIT_CONFIG, f"configuration error – {exc}")

    approver = DecoupledWaiter(config.tan.wait_minutes * 60, config.tan.poll_seconds, sleep, clock)
    source = source_factory(config.bank, pin, product_id, state.fints_client_data, approver)
    delivered: list[tuple[str, int]] = []
    failed: list[tuple[str, str]] = []
    try:
        with source:
            state.login_succeeded()
            for account in config.accounts:
                window = fetch_window(state, account, config.fetch, today)
                label = mask_iban(account.iban)
                try:
                    result = fetch_statement(
                        source, account, window, fetch_format=config.fetch.format, bank_bic=config.bank.bic
                    )
                    where = deliver_one(result, account)
                except (ApprovalPendingError, TanRequiredError, WrongPinError, StateError):
                    raise
                except Fints2CamtError as exc:
                    log.error("%s: %s", label, exc)
                    failed.append((label, str(exc)))
                    continue
                state.mark_delivered(account.iban, window.end)
                save_state(config.state_file, state)
                log.info(
                    "%s: %d entries %s..%s (%s) delivered: %s",
                    label, result.entry_count, window.start, window.end, result.source_format, where,
                )
                delivered.append((label, result.entry_count))
    except WrongPinError as exc:
        state.pin_rejected_since = now
        _save_after_dialog(config, state, source)
        return Outcome(EXIT_PIN, f"stopped – {exc}; fix the PIN, then run `fints2camt approve`")
    except LoginFailedError as exc:
        state.login_failed(now)
        _save_after_dialog(config, state, source)
        blocked = _blocked(state)
        if blocked is not None:
            return blocked
        return Outcome(
            EXIT_FAILED,
            f"failed – {exc} (refused start {state.failed_logins} of {MAX_FAILED_LOGINS}: after "
            f"{MAX_FAILED_LOGINS} in a row, runs stop until `fints2camt approve`; check the PIN)",
        )
    except (ApprovalPendingError, TanRequiredError) as exc:
        state.approval_needed_since = now
        _save_after_dialog(config, state, source)
        log.warning("%s", exc)
        return Outcome(EXIT_ACTION_NEEDED, _with_progress(APPROVE_HINT, delivered, len(config.accounts)))
    except StateError:
        raise
    except Fints2CamtError as exc:
        _save_after_dialog(config, state, source)
        return Outcome(EXIT_FAILED, _with_progress(f"failed – {exc}", delivered, len(config.accounts)))
    _save_after_dialog(config, state, source)

    entries = sum(count for _, count in delivered)
    progress = f"{len(delivered)}/{len(config.accounts)} accounts delivered, {entries} entries"
    if failed:
        details = "; ".join(f"{label}: {message}" for label, message in failed)
        return Outcome(EXIT_FAILED, f"failed – {progress}; {details}")
    return Outcome(EXIT_OK, f"ok – {progress}")


def _blocked(state: State) -> Outcome | None:
    if state.pin_rejected_since is not None:
        return Outcome(
            EXIT_PIN,
            f"stopped – the bank rejected the PIN at {state.pin_rejected_since:%Y-%m-%d %H:%M} UTC; "
            "the bank is not contacted again until you fixed the PIN and ran `fints2camt approve`",
        )
    if state.logins_exhausted:
        last = f", the last at {state.last_failed_login:%Y-%m-%d %H:%M} UTC" if state.last_failed_login else ""
        return Outcome(
            EXIT_PIN,
            f"stopped – the bank refused the start of the dialog {state.failed_logins} times in a row{last} "
            "(a wrong PIN?); the bank is not contacted again until you checked the PIN and ran "
            "`fints2camt approve`",
        )
    if state.approval_needed_since is not None:
        return Outcome(
            EXIT_ACTION_NEEDED,
            f"{APPROVE_HINT} (pending since {state.approval_needed_since:%Y-%m-%d %H:%M} UTC)",
        )
    return None


def _with_progress(message: str, delivered: list[tuple[str, int]], total: int) -> str:
    if not delivered:
        return message
    return f"{message} ({len(delivered)}/{total} accounts delivered before)"


def _save_after_dialog(config: Config, state: State, source: Any) -> None:
    """Keep the bank's client data (system id, TAN method) of the dialog, if it got that far."""
    client_data = getattr(source, "client_data", None)
    if client_data:
        state.fints_client_data = client_data
    save_state(config.state_file, state)


# --- setup and approve -----------------------------------------------------------------


def setup(
    config: Config,
    *,
    environ: Mapping[str, str] | None = None,
    prompt: Prompt = input,
    out: Output = print,
    now: datetime.datetime | None = None,
    source_factory: SourceFactory = default_source_factory,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """First login: TAN method selection (decoupled preferred), first approval, the state."""
    return _interactive(
        config, select_tan=True, environ=environ, prompt=prompt, out=out, now=now,
        source_factory=source_factory, sleep=sleep, clock=clock,
    )


def approve(
    config: Config,
    *,
    environ: Mapping[str, str] | None = None,
    prompt: Prompt = input,
    out: Output = print,
    now: datetime.datetime | None = None,
    source_factory: SourceFactory = default_source_factory,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the approval again, interactively; lets the next ``run`` contact the bank again."""
    return _interactive(
        config, select_tan=False, environ=environ, prompt=prompt, out=out, now=now,
        source_factory=source_factory, sleep=sleep, clock=clock,
    )


def _interactive(
    config: Config,
    *,
    select_tan: bool,
    environ: Mapping[str, str] | None,
    prompt: Prompt,
    out: Output,
    now: datetime.datetime | None,
    source_factory: SourceFactory,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    env = os.environ if environ is None else environ
    now = now or _now()
    try:
        with state_lock(config.state_file):
            return _interact(
                config, select_tan=select_tan, env=env, prompt=prompt, out=out, now=now,
                source_factory=source_factory, sleep=sleep, clock=clock,
            )
    except AlreadyRunningError as exc:
        out(f"failed – {exc}")
        return EXIT_FAILED


def _interact(
    config: Config,
    *,
    select_tan: bool,
    env: Mapping[str, str],
    prompt: Prompt,
    out: Output,
    now: datetime.datetime,
    source_factory: SourceFactory,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> int:
    state = load_state(config.state_file)
    if not select_tan and state.fints_client_data is None:
        out("not set up yet – run `fints2camt setup` first")
        return EXIT_ACTION_NEEDED
    try:
        pin = read_pin(config.bank, env)
        product_id = product_id_from_env(env)
    except ConfigError as exc:
        out(f"configuration error – {exc}")
        return EXIT_CONFIG

    # Interactive: the operator is there, so wait at least ten minutes.
    approver = InteractiveApprover(
        max(config.tan.wait_minutes, 10) * 60, config.tan.poll_seconds, prompt, out, sleep, clock
    )
    source = source_factory(config.bank, pin, product_id, state.fints_client_data, approver)
    mechanism: str | None = None
    try:
        if select_tan:
            mechanism = source.configure_tan(
                lambda mechanisms: choose_tan_mechanism(mechanisms, prompt, out),
                lambda media: choose_tan_medium(media, prompt, out),
            )
        with source:
            missing = [account.iban for account in config.accounts if not source.account_visible(account.iban)]
    except WrongPinError as exc:
        state.pin_rejected_since = now
        _save_after_dialog(config, state, source)
        out(f"stopped – {exc}; fix the PIN before you try again")
        return EXIT_PIN
    except LoginFailedError as exc:
        state.login_failed(now)
        _save_after_dialog(config, state, source)
        out(
            f"failed – {exc}; refused {state.failed_logins} time(s) in a row – check the PIN before "
            "you try again (banks lock the access after a few wrong PINs)"
        )
        return EXIT_PIN if state.logins_exhausted else EXIT_FAILED
    except (ApprovalPendingError, TanRequiredError) as exc:
        state.approval_needed_since = state.approval_needed_since or now
        _save_after_dialog(config, state, source)
        out(f"not approved – {exc}")
        return EXIT_ACTION_NEEDED

    state.bank_access_cleared()
    _save_after_dialog(config, state, source)
    if mechanism:
        out(f"TAN method: {mechanism}")
    for iban in missing:
        out(f"warning: the bank access does not see the configured account {mask_iban(iban)}")
    out("approved – the next `fints2camt run` fetches and delivers" if not missing else
        "approved, but configured accounts are missing (see above) – check the configuration")
    return EXIT_OK if not missing else EXIT_FAILED


# --- check -----------------------------------------------------------------------------


def check(
    config: Config,
    *,
    environ: Mapping[str, str] | None = None,
    out: Output = print,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Validate configuration, secrets and state, and that bank and targets answer.

    Opens no FinTS dialog (no PIN is sent, no TAN asked for) and sends no statement: the
    reachability check is a bare HTTPS request without credentials.
    """
    env = os.environ if environ is None else environ
    problems = 0

    def report(ok: bool, message: str) -> None:
        nonlocal problems
        problems += not ok
        out(f"{'ok  ' if ok else 'FAIL'} {message}")

    report(True, f"configuration: bank {config.bank.blz}, {len(config.accounts)} account(s)")
    for name, read in (
        ("FinTS product id", lambda: product_id_from_env(env)),
        ("PIN", lambda: read_pin(config.bank, env)),
    ):
        try:
            read()
            report(True, f"{name} is set")
        except ConfigError as exc:
            report(False, str(exc))

    for account in config.accounts:
        label = mask_iban(account.iban)
        target = account.target
        if target.folder is not None:
            ok, message = _folder_writable(target.folder)
            report(ok, f"{label}: folder {target.folder} {message}")
            continue
        try:
            read_token(target, env)
            report(True, f"{label}: {target.token_source} is set")
        except ConfigError as exc:
            report(False, f"{label}: {exc}")

    try:
        state = load_state(config.state_file)
    except StateError as exc:
        report(False, str(exc))
    else:
        if state.fints_client_data is None:
            report(False, "state: not set up yet – run `fints2camt setup`")
        else:
            blocked = _blocked(state)
            report(blocked is None, f"state: {blocked.summary if blocked else 'set up'}")

    urls = [config.bank.url] + [a.target.url for a in config.accounts if a.target.url is not None]
    for url in dict.fromkeys(urls):
        ok, message = _reachable(url, transport)
        report(ok, f"{url} {message}")

    out("all checks passed" if not problems else f"{problems} check(s) failed")
    return EXIT_OK if not problems else EXIT_FAILED


def _folder_writable(folder: Path) -> tuple[bool, str]:
    existing = folder
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not existing.is_dir():
        return False, "is not a directory"
    if not os.access(existing, os.W_OK | os.X_OK):
        return False, "is not writable"
    return True, "is writable" if existing == folder else "will be created"


def _reachable(url: str, transport: httpx.BaseTransport | None) -> tuple[bool, str]:
    try:
        with httpx.Client(transport=transport, timeout=15, follow_redirects=False) as client:
            response = client.head(url)
    except httpx.HTTPError as exc:
        return False, f"is not reachable: {type(exc).__name__}"
    return True, f"is reachable (HTTP {response.status_code})"
