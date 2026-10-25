"""The local state file and the fetch window it determines.

The state holds, per account, the last booking date that has been delivered successfully,
and the FinTS client data (system id, bank and user parameter data, the selected TAN
method) that lets the next dialog reuse the registration instead of asking for a new TAN.
It also records the conditions that stop ``run`` from contacting the bank until the
operator acted: an approval that did not arrive (no push spam), a rejected PIN and
:data:`MAX_FAILED_LOGINS` refused dialog starts in a row (no lock-out). It never holds
the PIN.

The file is written atomically and readable by its owner only: the FinTS client data
contains account numbers. A command that talks to the bank holds an exclusive lock on
``<state file>.lock`` (:func:`state_lock`), so two runs never open two dialogs at once.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_FIRST_RUN_DAYS, AccountConfig, FetchConfig
from .errors import AlreadyRunningError, StateError
from .model import FetchWindow

STATE_VERSION = 1
# Refused dialog starts in a row after which ``run`` no longer contacts the bank. Banks
# usually lock the access after three wrong PINs; the third attempt is the operator's.
MAX_FAILED_LOGINS = 2


@dataclass
class State:
    delivered_through: dict[str, datetime.date] = field(default_factory=dict)
    fints_client_data: bytes | None = None
    # Set when ``run`` gave up waiting for an approval; cleared by ``approve``/``setup``.
    approval_needed_since: datetime.datetime | None = None
    # Set when the bank rejected the PIN or reported the access locked; cleared likewise.
    pin_rejected_since: datetime.datetime | None = None
    # Dialog starts the bank refused in a row (any error before it accepted the PIN), and
    # when the last one was; reset by a dialog that started, cleared like the above.
    failed_logins: int = 0
    last_failed_login: datetime.datetime | None = None
    # Keys this version does not know, kept as they are on rewrite.
    extra: dict[str, Any] = field(default_factory=dict)

    def mark_delivered(self, iban: str, through: datetime.date) -> None:
        """Record a successful delivery up to ``through``; the date never moves backwards."""
        current = self.delivered_through.get(iban)
        if current is None or through > current:
            self.delivered_through[iban] = through

    def login_failed(self, when: datetime.datetime) -> None:
        self.failed_logins += 1
        self.last_failed_login = when

    def login_succeeded(self) -> None:
        self.failed_logins = 0
        self.last_failed_login = None

    @property
    def logins_exhausted(self) -> bool:
        return self.failed_logins >= MAX_FAILED_LOGINS

    def bank_access_cleared(self) -> None:
        """The operator completed ``approve`` or ``setup``: the bank may be contacted again."""
        self.approval_needed_since = None
        self.pin_rejected_since = None
        self.login_succeeded()


def load_state(path: Path) -> State:
    """Read the state file; a missing file is the empty state of a first run."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    except OSError as exc:
        raise StateError(f"cannot read the state file {path}: {exc.strerror}") from None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise ValueError(f"unsupported state version {data.get('version') if isinstance(data, dict) else None}")
        accounts = data.get("accounts", {})
        delivered = {
            iban: datetime.date.fromisoformat(entry["delivered_through"])
            for iban, entry in accounts.items()
            if entry.get("delivered_through")
        }
        fints = data.get("fints", {})
        client_data = fints.get("client_data")
        approval_needed = fints.get("approval_needed_since")
        pin_rejected = fints.get("pin_rejected_since")
        failed_logins = fints.get("failed_logins", 0)
        if isinstance(failed_logins, bool) or not isinstance(failed_logins, int) or failed_logins < 0:
            raise ValueError("failed_logins must be a non-negative integer")
        last_failed = fints.get("last_failed_login")
        extra = {key: value for key, value in data.items() if key not in ("version", "accounts", "fints")}
        return State(
            delivered_through=delivered,
            fints_client_data=base64.b64decode(client_data) if client_data else None,
            approval_needed_since=datetime.datetime.fromisoformat(approval_needed) if approval_needed else None,
            pin_rejected_since=datetime.datetime.fromisoformat(pin_rejected) if pin_rejected else None,
            failed_logins=failed_logins,
            last_failed_login=datetime.datetime.fromisoformat(last_failed) if last_failed else None,
            extra=extra,
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StateError(f"the state file {path} is corrupt: {exc}") from None


def save_state(path: Path, state: State) -> None:
    """Write the state atomically (temporary file + rename), mode 0600."""
    data: dict[str, Any] = dict(state.extra)
    data["version"] = STATE_VERSION
    data["accounts"] = {
        iban: {"delivered_through": date.isoformat()}
        for iban, date in sorted(state.delivered_through.items())
    }
    data["fints"] = {
        "client_data": base64.b64encode(state.fints_client_data).decode("ascii")
        if state.fints_client_data else None,
        "approval_needed_since": _timestamp(state.approval_needed_since),
        "pin_rejected_since": _timestamp(state.pin_rejected_since),
        "failed_logins": state.failed_logins,
        "last_failed_login": _timestamp(state.last_failed_login),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    except OSError as exc:
        raise StateError(f"cannot write the state file {path}: {exc.strerror}") from None


@contextlib.contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """Hold the exclusive lock of the state file ``path`` (on ``<path>.lock``), or raise
    :class:`AlreadyRunningError` at once when another process holds it.

    The lock file stays in place; the kernel releases the lock when the process ends,
    however it ends.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise StateError(f"cannot open the lock file {lock_path}: {exc.strerror}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunningError(
                f"another fints2camt is running (it holds {lock_path}); not starting a second bank dialog"
            ) from None
        except OSError as exc:
            raise StateError(f"cannot lock {lock_path}: {exc.strerror}") from None
        yield
    finally:
        os.close(fd)


def _timestamp(value: datetime.datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def fetch_window(
    state: State,
    account: AccountConfig,
    fetch: FetchConfig,
    today: datetime.date,
) -> FetchWindow:
    """The booking dates to fetch for ``account``.

    After a successful delivery: from the last delivered booking date minus the overlap
    (targets deduplicate the repeated days, and late bookings with an earlier booking
    date are caught). On the first run: from the configured start date (the account's,
    else the global one, else ``DEFAULT_FIRST_RUN_DAYS`` back). Always up to ``today``.
    """
    delivered = state.delivered_through.get(account.iban)
    if delivered is not None:
        start = delivered - datetime.timedelta(days=fetch.overlap_days)
    else:
        start = (
            account.start_date
            or fetch.start_date
            or today - datetime.timedelta(days=DEFAULT_FIRST_RUN_DAYS)
        )
    return FetchWindow(start=min(start, today), end=today)
