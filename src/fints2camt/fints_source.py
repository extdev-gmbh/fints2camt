"""The bank side: a FinTS dialog (python-fints) that fetches camt, MT940 and balances.

The FinTS product registration number is read from ``FINTS_PRODUCT_ID`` and never stored
in the repository. The PIN is handed to python-fints only, which sends it to the bank's
FinTS URL; it is not logged and not kept in the state.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from types import TracebackType
from typing import Any, Protocol

from fints.client import FinTS3PinTanClient, NeedTANResponse
from fints.exceptions import (
    FinTSClientPINError,
    FinTSClientTemporaryAuthError,
    FinTSConnectionError,
    FinTSError,
    FinTSSCARequiredError,
    FinTSUnsupportedOperation,
)
from fints.segments.statement import HKKAZ5, HKKAZ6, HKKAZ7

from . import __version__
from .config import BankConfig, mask_iban
from .errors import (
    AccountNotFoundError,
    ApprovalPendingError,
    BankError,
    ConfigError,
    LoginFailedError,
    TanRequiredError,
    WrongPinError,
)
from .model import Balance, FetchWindow
from .tan import Approver

PRODUCT_ID_ENV = "FINTS_PRODUCT_ID"


class BankSource(Protocol):
    """What the conversion needs from the bank, per account."""

    def supports_camt(self) -> bool: ...

    def fetch_camt(self, iban: str, window: FetchWindow) -> list[bytes]: ...

    def fetch_mt940(self, iban: str, window: FetchWindow) -> str: ...

    def fetch_balance(self, iban: str) -> Balance | None: ...


def product_id_from_env(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    product_id = env.get(PRODUCT_ID_ENV, "").strip()
    if not product_id:
        raise ConfigError(
            f"{PRODUCT_ID_ENV} is not set: register a FinTS product with the Deutsche Kreditwirtschaft "
            "and put its registration number into this environment variable"
        )
    return product_id


@contextlib.contextmanager
def _bank_errors() -> Iterator[None]:
    """Translate python-fints errors; a rejected PIN becomes :class:`WrongPinError`."""
    try:
        yield
    except FinTSClientPINError:
        raise WrongPinError("the bank rejected the PIN; not retrying to avoid a lock-out") from None
    except FinTSClientTemporaryAuthError:
        raise WrongPinError("the bank reports the online-banking access as locked") from None
    except FinTSSCARequiredError:
        raise ApprovalPendingError("the bank requires a strong customer authentication") from None
    except FinTSUnsupportedOperation as exc:
        raise BankError(f"the bank does not support the operation: {exc}") from exc
    except FinTSError as exc:
        raise BankError(f"FinTS error: {exc}") from exc


class FinTSSource:
    """A FinTS PIN/TAN dialog with the bank, used as a context manager.

    When the bank asks for a TAN — at dialog start or for an operation — the ``approver``
    (see :mod:`fints2camt.tan`) completes the approval or raises; without an approver
    every fetch raises :class:`TanRequiredError` instead. After the dialog,
    ``client_data`` holds the python-fints data blob (system id, BPD, UPD, the selected
    TAN method) to store in the state for the next run.
    """

    def __init__(
        self,
        bank: BankConfig,
        pin: str,
        product_id: str,
        client_data: bytes | None = None,
        client_factory: Callable[..., Any] = FinTS3PinTanClient,
        approver: Approver | None = None,
    ) -> None:
        self._bank = bank
        self._pin = pin
        self._product_id = product_id
        self._client_data_in = client_data
        self._client_factory = client_factory
        self._approver = approver
        self._built: Any = None
        self._client: Any = None
        self._accounts: dict[str, Any] | None = None
        self.client_data: bytes | None = None

    def __repr__(self) -> str:
        return f"FinTSSource(blz={self._bank.blz!r})"

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("the FinTS dialog is not open")
        return self._client

    @property
    def pending_challenge(self) -> Any:
        response = getattr(self._client, "init_tan_response", None)
        return response if isinstance(response, NeedTANResponse) else None

    def _build(self) -> Any:
        if self._built is None:
            with _bank_errors():
                self._built = self._client_factory(
                    bank_identifier=self._bank.blz,
                    user_id=self._bank.user_id,
                    pin=self._pin,
                    server=self._bank.url,
                    customer_id=self._bank.customer_id,
                    product_id=self._product_id,
                    product_version=__version__[:5],
                    from_data=self._client_data_in,
                )
        return self._built

    def configure_tan(
        self,
        choose_mechanism: Callable[[Mapping[str, Any]], str],
        choose_medium: Callable[[list[Any]], Any],
    ) -> str | None:
        """Select the TAN method (and medium) before the dialog; returns the method's name.

        Talks to the bank (synchronisation, TAN media), so it is for ``setup`` only.
        """
        if self._client is not None:
            raise RuntimeError("the TAN method cannot change while the FinTS dialog is open")
        client = self._build()
        with _bank_errors():
            client.fetch_tan_mechanisms()
            mechanisms = client.get_tan_mechanisms()
            if not mechanisms:
                return None
            function = choose_mechanism(mechanisms)
            client.set_tan_mechanism(function)
            if client.is_tan_media_required():
                _usage, media = client.get_tan_media()
                if media:
                    client.set_tan_medium(choose_medium(list(media)))
                else:
                    # Some banks (pushTAN) answer the media query with an approval request
                    # and accept an empty medium name.
                    client.selected_tan_medium = ""
        return str(getattr(mechanisms[function], "name", None) or function)

    def __enter__(self) -> FinTSSource:
        """Open the dialog; a refused start raises :class:`LoginFailedError`.

        Any python-fints error while the dialog starts — before the bank accepted the PIN —
        may be a rejected PIN in disguise (banks use various codes for it), so it is not a
        plain, retryable :class:`BankError`. Only a connection error (no answer, or an HTTP
        error status) is: the PIN did not reach a bank that judged it.
        """
        client = self._build()
        # A challenge left over from a dialog of configure_tan() is not this dialog's.
        client.init_tan_response = None
        try:
            with _bank_errors():
                client.__enter__()
        except (WrongPinError, ApprovalPendingError):
            raise
        except BankError as exc:
            if isinstance(exc.__cause__, FinTSConnectionError) or not isinstance(exc.__cause__, FinTSError):
                raise
            raise LoginFailedError(f"the bank refused the start of the dialog: {exc.__cause__}") from exc.__cause__
        self._client = client
        challenge = self.pending_challenge
        if challenge is not None and self._approver is not None:
            try:
                self._approve(challenge)
                client.init_tan_response = None
            except BaseException:
                self.__exit__(*sys.exc_info())
                raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        self._built = None
        try:
            # While an error is on its way, a failing dialog end must not replace it.
            with contextlib.suppress(BankError) if exc is not None else contextlib.nullcontext():
                with _bank_errors():
                    client.__exit__(exc_type, exc, traceback)
        finally:
            self.client_data = client.deconstruct(including_private=True)
            self._client_data_in = self.client_data

    def _approve(self, challenge: Any) -> Any:
        """Complete the challenge with the approver.

        Once the bank sent a challenge, every failure before the approval completes — a
        dialog error, a declined approval, a missing status — ends as
        :class:`ApprovalPendingError`, so that ``run`` records it and starts no further
        challenge (no push spam). Only a rejected PIN keeps its own path.
        """
        assert self._approver is not None
        try:
            with _bank_errors():
                return self._approver(challenge, self.client.send_tan)
        except (WrongPinError, ApprovalPendingError):
            raise
        except Exception as exc:
            raise ApprovalPendingError(f"the approval did not complete: {exc}") from exc

    def _check_tan(self) -> None:
        challenge = self.pending_challenge
        if challenge is not None:
            raise TanRequiredError("the bank asks for a TAN approval", challenge=challenge)

    def _resolve(self, response: Any) -> Any:
        """The operation's response; a TAN request in between goes to the approver."""
        if not isinstance(response, NeedTANResponse):
            return response
        if self._approver is None:
            raise TanRequiredError("the bank asks for a TAN approval", challenge=response)
        return self._approve(response)

    def _account(self, iban: str) -> Any:
        self._check_tan()
        if self._accounts is None:
            with _bank_errors():
                accounts = self.client.get_sepa_accounts()
            accounts = self._resolve(accounts)
            self._accounts = {account.iban.replace(" ", "").upper(): account for account in accounts}
        try:
            return self._accounts[iban]
        except KeyError:
            raise AccountNotFoundError(f"the bank access does not see the account {mask_iban(iban)}") from None

    def account_visible(self, iban: str) -> bool:
        try:
            self._account(iban)
        except AccountNotFoundError:
            return False
        return True

    def supports_camt(self) -> bool:
        return self.client.bpd.find_segment_first("HICAZS") is not None

    def fetch_camt(self, iban: str, window: FetchWindow) -> list[bytes]:
        """The bank's booked camt documents for the window (HKCAZ); pending entries are dropped."""
        account = self._account(iban)
        with _bank_errors():
            response = self.client.get_transactions_xml(account, window.start, window.end)
        response = self._resolve(response)
        booked, _pending = response
        return [bytes(document) for document in booked]

    def fetch_mt940(self, iban: str, window: FetchWindow) -> str:
        """The bank's booked MT940 for the window (HKKAZ), unparsed.

        python-fints only offers parsed MT940 objects; the raw text is fetched with the same
        (internal) calls its ``get_transactions`` uses — python-fints is pinned for this.
        """
        account = self._account(iban)
        client = self.client
        with _bank_errors():
            with client._get_dialog() as dialog:
                hkkaz = client._find_highest_supported_command(HKKAZ5, HKKAZ6, HKKAZ7)
                response = client._fetch_with_touchdowns(
                    dialog,
                    lambda touchdown: hkkaz(
                        account=hkkaz._fields["account"].type.from_sepa_account(account),
                        all_accounts=False,
                        date_start=window.start,
                        date_end=window.end,
                        touchdown_point=touchdown,
                    ),
                    # MT940 uses the SWIFT character set, a subset of ISO 8859.
                    lambda segments: "".join(seg.statement_booked.decode("iso-8859-1") for seg in segments),
                    "HIKAZ",
                )
            response = self._resolve(response)
        return str(response)

    def fetch_balance(self, iban: str) -> Balance | None:
        """The booked balance (HKSAL), or ``None`` when the bank does not offer it."""
        account = self._account(iban)
        try:
            with _bank_errors():
                response = self.client.get_balance(account)
        except BankError as exc:
            if isinstance(exc.__cause__, FinTSUnsupportedOperation):
                return None
            raise
        response = self._resolve(response)
        if response is None:
            return None
        amount = response.amount
        return Balance(amount=Decimal(str(amount.amount)), currency=str(amount.currency), date=response.date)
