import datetime
from decimal import Decimal

import pytest

from fints2camt import camt
from fints2camt.config import AccountConfig, TargetConfig
from fints2camt.errors import BankError, CamtError
from fints2camt.fetch import fetch_statement
from fints2camt.model import Balance, FetchWindow

from conftest import NS, OWN_IBAN, fixture_bytes, fixture_text, parse_xml

ACCOUNT = AccountConfig(iban=OWN_IBAN, target=TargetConfig(folder=None))
WINDOW = FetchWindow(datetime.date(2026, 10, 21), datetime.date(2026, 10, 25))
CREATED = datetime.datetime(2026, 10, 25, 6, 0, tzinfo=datetime.timezone.utc)


class FakeSource:
    def __init__(self, camt_docs=None, mt940_text="", balance=None):
        self.camt_docs = camt_docs
        self.mt940_text = mt940_text
        self.balance = balance
        self.calls: list[str] = []

    def supports_camt(self) -> bool:
        return self.camt_docs is not None

    def fetch_camt(self, iban, window):
        self.calls.append("camt")
        return self.camt_docs

    def fetch_mt940(self, iban, window):
        self.calls.append("mt940")
        return self.mt940_text

    def fetch_balance(self, iban):
        self.calls.append("balance")
        return self.balance


def balances(document: bytes) -> dict[str, tuple[str, str, str]]:
    root = parse_xml(document)
    return {
        bal.findtext("c:Tp/c:CdOrPrtry/c:Cd", namespaces=NS): (
            bal.findtext("c:Amt", namespaces=NS),
            bal.findtext("c:CdtDbtInd", namespaces=NS),
            bal.findtext("c:Dt/c:Dt", namespaces=NS),
        )
        for bal in root.findall(".//c:Bal", NS)
    }


def test_mt940_is_converted_with_statement_balances() -> None:
    source = FakeSource(mt940_text=fixture_text("mt940_synthetic.sta"))
    result = fetch_statement(source, ACCOUNT, WINDOW, created=CREATED)
    assert result.source_format == "mt940"
    assert result.entry_count == 5
    assert source.calls == ["mt940"]  # the MT940 balances suffice, no HKSAL needed
    camt.validate_camt053(result.document)
    assert balances(result.document) == {
        "OPBD": ("1000.00", "CRDT", "2026-10-21"),
        "CLBD": ("945.00", "CRDT", "2026-10-25"),
    }
    root = parse_xml(result.document)
    refs = [n.findtext("c:NtryRef", namespaces=NS) for n in root.findall(".//c:Ntry", NS)]
    assert len(set(refs)) == 5


def test_auto_prefers_camt_when_offered() -> None:
    source = FakeSource(camt_docs=[fixture_bytes("camt052_v02_synthetic.xml")], mt940_text="unused")
    result = fetch_statement(source, ACCOUNT, WINDOW, created=CREATED, bank_bic="COBADEFFXXX")
    assert result.source_format == "camt"
    assert source.calls == ["camt"]
    assert result.entry_count == 2
    assert balances(result.document)["CLBD"] == ("1200.01", "CRDT", "2026-10-25")
    assert parse_xml(result.document).findtext(".//c:Acct/c:Svcr/c:FinInstnId/c:BIC", namespaces=NS) == "COBADEFFXXX"


def test_format_mt940_forces_mt940() -> None:
    source = FakeSource(camt_docs=[], mt940_text=fixture_text("mt940_synthetic.sta"))
    assert fetch_statement(source, ACCOUNT, WINDOW, fetch_format="mt940").source_format == "mt940"


def test_format_camt_without_bank_support_fails() -> None:
    with pytest.raises(BankError):
        fetch_statement(FakeSource(), ACCOUNT, WINDOW, fetch_format="camt")


def test_hksal_closing_and_derived_opening_without_statement_balances() -> None:
    # camt without balances (v08 fixture): closing from HKSAL, opening = closing - entries
    source = FakeSource(
        camt_docs=[fixture_bytes("camt052_v08_synthetic.xml")],
        balance=Balance(Decimal("-120.00"), "EUR", datetime.date(2026, 10, 25)),
    )
    result = fetch_statement(source, ACCOUNT, WINDOW, created=CREATED)
    assert source.calls == ["camt", "balance"]
    # entries: -300.00 and +49.99 -> net -250.01
    assert balances(result.document) == {
        "OPBD": ("130.01", "CRDT", "2026-10-21"),
        "CLBD": ("120.00", "DBIT", "2026-10-25"),
    }


def test_no_transactions_uses_hksal_for_both_balances() -> None:
    source = FakeSource(mt940_text="", balance=Balance(Decimal("500.00"), "EUR", datetime.date(2026, 10, 25)))
    result = fetch_statement(source, ACCOUNT, WINDOW, created=CREATED)
    assert result.entry_count == 0
    assert balances(result.document)["OPBD"][0] == balances(result.document)["CLBD"][0] == "500.00"


def test_no_balance_at_all_fails() -> None:
    with pytest.raises(CamtError):
        fetch_statement(FakeSource(mt940_text=""), ACCOUNT, WINDOW)


def test_overlapping_windows_give_the_same_entry_references() -> None:
    text = fixture_text("mt940_synthetic.sta")
    later = FetchWindow(datetime.date(2026, 10, 22), datetime.date(2026, 10, 26))
    first = parse_xml(fetch_statement(FakeSource(mt940_text=text), ACCOUNT, WINDOW).document)
    second = parse_xml(fetch_statement(FakeSource(mt940_text=text), ACCOUNT, later).document)
    refs = lambda root: [n.findtext("c:NtryRef", namespaces=NS) for n in root.findall(".//c:Ntry", NS)]
    assert refs(first) == refs(second)


def test_identical_bookings_on_one_day_get_distinct_references() -> None:
    text = (
        ":20:X\n:60F:C261024EUR0,00\n"
        ":61:2610241024D1,00NMSCNONREF\n:86:Parking\n"
        ":61:2610241024D1,00NMSCNONREF\n:86:Parking\n"
        ":62F:D261024EUR2,00\n-"
    )
    document = fetch_statement(FakeSource(mt940_text=text), ACCOUNT, WINDOW).document
    root = parse_xml(document)
    refs = [n.findtext("c:NtryRef", namespaces=NS) for n in root.findall(".//c:Ntry", NS)]
    assert len(refs) == 2 and refs[0] != refs[1]
    assert balances(document)["CLBD"] == ("2.00", "DBIT", "2026-10-25")
