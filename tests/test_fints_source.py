"""FinTSSource against a mocked python-fints client (no network, synthetic data only)."""

import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fints.exceptions import (
    FinTSClientError,
    FinTSClientPINError,
    FinTSConnectionError,
    FinTSDialogError,
    FinTSDialogInitError,
    FinTSNoResponseError,
    FinTSSCARequiredError,
    FinTSUnsupportedOperation,
)

from fints2camt.config import BankConfig
from fints2camt.errors import (
    AccountNotFoundError,
    ApprovalPendingError,
    BankError,
    ConfigError,
    LoginFailedError,
    TanRequiredError,
    WrongPinError,
)
from fints2camt.fints_source import FinTSSource, product_id_from_env
from fints2camt.model import Balance, FetchWindow

from conftest import OWN_IBAN
from fake_fints import OTHER_IBAN, FakeClient, client_factory, tan_challenge

BANK = BankConfig(blz="37040044", url="https://fints.example-bank.invalid/fints", user_id="synthetic-user")
WINDOW = FetchWindow(datetime.date(2026, 10, 21), datetime.date(2026, 10, 25))


def source(approver=None, **overrides) -> tuple[FinTSSource, list[FakeClient]]:
    clients: list[FakeClient] = []
    fints = FinTSSource(
        BANK, "1234", "SYNTHETICPRODUCTID", client_data=b"old-blob",
        client_factory=client_factory(clients, **overrides), approver=approver,
    )
    return fints, clients


class RecordingApprover:
    """Approves with the given answer (or raises it) and records the challenges."""

    def __init__(self, answer=None):
        self.answer = answer
        self.challenges = []

    def __call__(self, challenge, send_tan):
        self.challenges.append(challenge)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_product_id_comes_from_the_environment() -> None:
    assert product_id_from_env({"FINTS_PRODUCT_ID": " ABC123 "}) == "ABC123"
    with pytest.raises(ConfigError):
        product_id_from_env({})


def test_client_is_built_from_config_and_state_and_saves_its_data() -> None:
    fints, clients = source()
    with fints:
        client = clients[0]
        assert client.open
        assert client.kwargs["bank_identifier"] == "37040044"
        assert client.kwargs["server"] == BANK.url
        assert client.kwargs["product_id"] == "SYNTHETICPRODUCTID"
        assert client.kwargs["from_data"] == b"old-blob"
        assert client.kwargs["pin"] == "1234"
    assert not client.open
    assert fints.client_data == b"client-blob"
    assert "1234" not in repr(fints)


def test_camt_fetch_returns_booked_documents_only() -> None:
    fints, clients = source()
    with fints:
        assert fints.supports_camt()
        assert fints.fetch_camt(OWN_IBAN, WINDOW) == [b"<booked/>"]
    assert clients[0].sent == [("HKCAZ", OWN_IBAN, WINDOW.start, WINDOW.end)]


def test_mt940_fetch_returns_the_raw_text() -> None:
    fints, clients = source()
    with fints:
        assert fints.fetch_mt940(OWN_IBAN, WINDOW) == ":20:A\n:86:M\xfcller\n"
    (_, kwargs), = clients[0].sent
    assert kwargs["account"] == ("KTI", OWN_IBAN)
    assert (kwargs["date_start"], kwargs["date_end"]) == (WINDOW.start, WINDOW.end)
    assert kwargs["all_accounts"] is False


def test_balance_is_converted_to_a_signed_decimal() -> None:
    fints, _ = source()
    with fints:
        assert fints.fetch_balance(OWN_IBAN) == Balance(Decimal("-10.5"), "EUR", datetime.date(2026, 10, 25))


def test_balance_unsupported_is_none() -> None:
    fints, _ = source(balance_response=None)
    with fints:
        assert fints.fetch_balance(OWN_IBAN) is None


def test_unknown_account() -> None:
    fints, _ = source()
    with fints, pytest.raises(AccountNotFoundError) as excinfo:
        fints.fetch_camt("DE02120300000000202051", WINDOW)
    assert str(excinfo.value) == "the bank access does not see the account DE02…2051"


def test_wrong_pin_is_not_retried() -> None:
    fints, clients = source(fail_pin=True)
    with pytest.raises(WrongPinError) as excinfo:
        with fints:
            pass
    assert len(clients) == 1
    assert "1234" not in str(excinfo.value)


@pytest.mark.parametrize(
    "error",
    [
        FinTSClientError("Error during dialog initialization, could not fetch BPD. Please check the passed BLZ and FinTS url."),
        FinTSDialogInitError("Dialog initialization failed"),
        FinTSUnsupportedOperation("No supported HKVVB version found"),
        FinTSNoResponseError("no answer"),
    ],
)
def test_any_fints_error_at_dialog_start_is_a_failed_login(error: Exception) -> None:
    # Banks answer a wrong PIN with various codes (9010, 9050, 9800 …), not only the ones
    # python-fints maps to FinTSClientPINError.
    fints, clients = source(fail_init=error)
    with pytest.raises(LoginFailedError) as excinfo:
        with fints:
            pass
    assert excinfo.value.__cause__ is error
    assert "1234" not in str(excinfo.value)
    assert fints.client_data is None
    assert len(clients) == 1


def test_a_connection_error_at_dialog_start_stays_a_retryable_bank_error() -> None:
    fints, _ = source(fail_init=FinTSConnectionError("Could not connect to FinTS endpoint"))
    with pytest.raises(BankError) as excinfo:
        with fints:
            pass
    assert not isinstance(excinfo.value, LoginFailedError)


def test_tan_at_dialog_start_blocks_every_fetch() -> None:
    challenge = tan_challenge()
    fints, _ = source(init_challenge=challenge)
    with fints:
        assert fints.pending_challenge is challenge
        with pytest.raises(TanRequiredError) as excinfo:
            fints.fetch_mt940(OWN_IBAN, WINDOW)
    assert excinfo.value.challenge is challenge


def test_tan_for_an_operation() -> None:
    challenge = tan_challenge()
    fints, _ = source(xml_response=challenge)
    with fints, pytest.raises(TanRequiredError) as excinfo:
        fints.fetch_camt(OWN_IBAN, WINDOW)
    assert excinfo.value.challenge is challenge


def test_other_fints_errors_become_bank_errors() -> None:
    fints, clients = source()
    with fints:
        clients[0].get_sepa_accounts = lambda: (_ for _ in ()).throw(FinTSUnsupportedOperation("no HKSPA"))
        with pytest.raises(BankError):
            fints.fetch_camt(OWN_IBAN, WINDOW)


def test_dialog_start_approval_goes_to_the_approver() -> None:
    challenge = tan_challenge()
    approver = RecordingApprover(answer="dialog continued")
    fints, clients = source(approver=approver, init_challenge=challenge)
    with fints:
        assert approver.challenges == [challenge]
        assert fints.pending_challenge is None
        assert fints.fetch_camt(OWN_IBAN, WINDOW) == [b"<booked/>"]
    assert clients[0].entered == 1


def test_operation_approval_returns_the_operations_response() -> None:
    challenge = tan_challenge()
    approver = RecordingApprover(answer=([b"<approved/>"], []))
    fints, _ = source(approver=approver, xml_response=challenge)
    with fints:
        assert fints.fetch_camt(OWN_IBAN, WINDOW) == [b"<approved/>"]
    assert approver.challenges == [challenge]


def test_mt940_operation_approval() -> None:
    approver = RecordingApprover(answer=":20:B\n")
    fints, _ = source(approver=approver, kaz_challenge=tan_challenge())
    with fints:
        assert fints.fetch_mt940(OWN_IBAN, WINDOW) == ":20:B\n"


def test_failed_dialog_start_approval_ends_the_dialog_and_keeps_the_client_data() -> None:
    approver = RecordingApprover(answer=ApprovalPendingError("not in time"))
    fints, clients = source(approver=approver, init_challenge=tan_challenge())
    with pytest.raises(ApprovalPendingError):
        with fints:
            pytest.fail("the body must not run without the approval")
    assert not clients[0].open
    assert fints.client_data == b"client-blob"


@pytest.mark.parametrize(
    "error", [FinTSClientError("No TAN status received."), FinTSDialogError("9999 declined"), ValueError("garbled")]
)
def test_any_failure_after_the_challenge_means_approval_needed(error: Exception) -> None:
    fints, clients = source(approver=RecordingApprover(answer=error), init_challenge=tan_challenge())
    with pytest.raises(ApprovalPendingError, match="the approval did not complete"):
        with fints:
            pytest.fail("the body must not run without the approval")
    assert not clients[0].open

    fints, _ = source(approver=RecordingApprover(answer=error), kaz_challenge=tan_challenge())
    with fints:
        with pytest.raises(ApprovalPendingError, match="the approval did not complete"):
            fints.fetch_mt940(OWN_IBAN, WINDOW)


def test_a_rejected_pin_while_approving_stays_a_wrong_pin() -> None:
    approver = RecordingApprover(answer=FinTSClientPINError("PIN wrong?"))
    fints, _ = source(approver=approver, init_challenge=tan_challenge())
    with pytest.raises(WrongPinError):
        with fints:
            pytest.fail("the body must not run without the approval")


def test_account_visible() -> None:
    fints, _ = source()
    with fints:
        assert fints.account_visible(OWN_IBAN)
        assert not fints.account_visible(OTHER_IBAN)


def test_configure_tan_selects_method_and_medium_before_the_dialog() -> None:
    medium = SimpleNamespace(tan_medium_name="Synthetic Phone")
    fints, clients = source(media_required=True, media=[medium])
    offered = []

    def choose(mechanisms):
        offered.append(list(mechanisms))
        return "921"

    assert fints.configure_tan(choose, lambda media: media[0]) == "pushTAN 2.0"
    client = clients[0]
    assert offered == [["921", "910"]]
    assert (client.selected_security_function, client.selected_tan_medium) == ("921", "Synthetic Phone")
    with fints:
        assert clients == [client]  # the dialog uses the configured client


def test_configure_tan_with_an_empty_media_list_uses_an_empty_medium() -> None:
    fints, clients = source(media_required=True, media=[])
    fints.configure_tan(lambda mechanisms: "921", lambda media: pytest.fail("nothing to choose"))
    assert clients[0].selected_tan_medium == ""


def test_sca_required_outside_a_tan_flow_means_approval_needed() -> None:
    fints, clients = source()
    with fints:
        clients[0].get_sepa_accounts = lambda: (_ for _ in ()).throw(FinTSSCARequiredError("9075"))
        with pytest.raises(ApprovalPendingError):
            fints.fetch_camt(OWN_IBAN, WINDOW)


def test_a_failing_dialog_end_does_not_hide_the_error_on_its_way() -> None:
    fints, clients = source()

    def end_fails(*exc):
        raise FinTSSCARequiredError("dialog already closed by the bank")

    with pytest.raises(AccountNotFoundError):
        with fints:
            clients[0].__exit__ = end_fails
            fints.fetch_camt(OTHER_IBAN, WINDOW)
    assert fints.client_data == b"client-blob"
