"""A mocked python-fints client: no network, synthetic data only."""

from __future__ import annotations

import contextlib
import datetime
from types import SimpleNamespace
from typing import Any

from fints.client import NeedTANResponse
from fints.exceptions import FinTSClientPINError, FinTSUnsupportedOperation
from fints.models import SEPAAccount
from mt940.models import Balance as Mt940Balance

from conftest import OWN_IBAN

OTHER_IBAN = "DE02120300000000202051"


class FakeHKKAZ:
    _fields = {"account": SimpleNamespace(type=SimpleNamespace(from_sepa_account=lambda a: ("KTI", a.iban)))}

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def tan_challenge(decoupled: bool = True, text: str = "Please approve in your app", matrix: Any = None) -> NeedTANResponse:
    challenge = NeedTANResponse.__new__(NeedTANResponse)
    challenge.decoupled = decoupled
    challenge.challenge = text
    challenge.challenge_matrix = matrix
    challenge.challenge_hhduc = None
    return challenge


def mechanism(name: str, decoupled: bool) -> SimpleNamespace:
    return SimpleNamespace(name=name, zka_id="Decoupled" if decoupled else "HHD", decoupled_max_poll_number=60 if decoupled else None)


class FakeClient:
    """Records what is sent; the attributes below steer the bank's answers."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.init_tan_response: Any = None
        self.init_challenge: Any = None  # set as init_tan_response when the dialog starts
        self.open = False
        self.entered = 0
        self.camt_supported = True
        self.fail_pin = False
        self.fail_init: Exception | None = None  # raised when the dialog starts
        self.balance_response: Any = Mt940Balance("D", "10.5", datetime.date(2026, 10, 25), currency="EUR")
        self.xml_response: Any = ([b"<booked/>"], [b"<pending/>"])
        self.kaz_segments = [
            SimpleNamespace(statement_booked=":20:A\n".encode("iso-8859-1")),
            SimpleNamespace(statement_booked=":86:M\xfcller\n".encode("iso-8859-1")),
        ]
        self.kaz_challenge: Any = None  # a TAN request for the MT940 fetch
        self.accounts = [OWN_IBAN]
        self.sent: list[Any] = []
        # send_tan: each call pops the next answer; a NeedTANResponse means "not yet approved",
        # an exception is raised
        self.tan_answers: list[Any] = []
        self.tans_sent: list[tuple[Any, str]] = []
        # TAN method selection
        self.mechanisms = {"921": mechanism("pushTAN 2.0", True), "910": mechanism("chipTAN", False)}
        self.selected_security_function: str | None = None
        self.selected_tan_medium: str | None = None
        self.media_required = False
        self.media: list[Any] = []
        self.bpd = SimpleNamespace(
            find_segment_first=lambda name: object() if name == "HICAZS" and self.camt_supported else None
        )

    def __enter__(self) -> None:
        self.entered += 1
        if self.fail_pin:
            raise FinTSClientPINError("Error during dialog initialization, PIN wrong?")
        if self.fail_init is not None:
            raise self.fail_init
        self.open = True
        if self.init_challenge is not None:
            self.init_tan_response = self.init_challenge

    def __exit__(self, *exc: Any) -> None:
        self.open = False

    def deconstruct(self, including_private: bool = False) -> bytes:
        assert including_private
        return b"client-blob"

    def send_tan(self, challenge: Any, tan: str) -> Any:
        assert self.open, "send_tan outside the dialog"
        self.tans_sent.append((challenge, tan))
        answer = self.tan_answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def fetch_tan_mechanisms(self) -> None:
        self.sent.append("fetch_tan_mechanisms")

    def get_tan_mechanisms(self) -> dict[str, Any]:
        return self.mechanisms

    def set_tan_mechanism(self, function: str) -> None:
        assert not self.open
        self.selected_security_function = function

    def is_tan_media_required(self) -> bool:
        return self.media_required

    def get_tan_media(self) -> tuple[Any, list[Any]]:
        return None, self.media

    def set_tan_medium(self, medium: Any) -> None:
        self.selected_tan_medium = medium.tan_medium_name

    def get_sepa_accounts(self) -> list[SEPAAccount]:
        return [
            SEPAAccount(iban=iban, bic="COBADEFFXXX", accountnumber="0532013000", subaccount=None, blz="37040044")
            for iban in self.accounts
        ]

    def get_transactions_xml(self, account: SEPAAccount, start: Any, end: Any) -> Any:
        self.sent.append(("HKCAZ", account.iban, start, end))
        return self.xml_response

    def get_balance(self, account: SEPAAccount) -> Any:
        if self.balance_response is None:
            raise FinTSUnsupportedOperation("No supported HISALS version found.")
        return self.balance_response

    @contextlib.contextmanager
    def _get_dialog(self) -> Any:
        yield "dialog"

    def _find_highest_supported_command(self, *classes: Any) -> type[FakeHKKAZ]:
        return FakeHKKAZ

    def _fetch_with_touchdowns(self, dialog: Any, factory: Any, processor: Any, *args: Any) -> Any:
        segment = factory(None)
        self.sent.append(("HKKAZ", segment.kwargs))
        if self.kaz_challenge is not None:
            return self.kaz_challenge
        return processor(self.kaz_segments)


def client_factory(clients: list[FakeClient], **overrides: Any) -> Any:
    """A python-fints client factory that records the clients it builds."""

    def factory(**kwargs: Any) -> FakeClient:
        client = FakeClient(**kwargs)
        for key, value in overrides.items():
            setattr(client, key, value)
        clients.append(client)
        return client

    return factory
