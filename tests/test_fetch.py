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


# --- a booked entry dated after the window end (a weekend fee booked on the next business day)

FORWARD_NTRY = """
      <Ntry>
        <Amt Ccy="EUR">6.00</Amt>
        <CdtDbtInd>DBIT</CdtDbtInd>
        <Sts>BOOK</Sts>
        <BookgDt><Dt>2026-10-27</Dt></BookgDt>
        <ValDt><Dt>2026-10-27</Dt></ValDt>
        <BkTxCd><Prtry><Cd>NCHG+805+000</Cd><Issr>DK</Issr></Prtry></BkTxCd>
        <AddtlNtryInf>ENTGELTABSCHLUSS</AddtlNtryInf>
      </Ntry>
"""
FORWARD_MT940 = (
    ":20:STARTUMSE\n:25:37040044/0532013000\n:28C:00000/003\n:60F:C261024EUR945,00\n"
    ":61:2610271027D6,00NCHGNONREF\n:86:Entgeltabschluss\n:62F:C261027EUR939,00\n-"
)


def camt_with_forward_entry() -> bytes:
    text = fixture_text("camt052_v02_synthetic.xml")
    marker = "    </Rpt>"
    return text.replace(marker, FORWARD_NTRY + marker).encode("utf-8")


def period(document: bytes) -> tuple[str, str]:
    root = parse_xml(document)
    return (
        root.findtext(".//c:Stmt/c:FrToDt/c:FrDtTm", namespaces=NS),
        root.findtext(".//c:Stmt/c:FrToDt/c:ToDtTm", namespaces=NS),
    )


def entry_references(document: bytes) -> dict[str, str]:
    """``NtryRef`` per booking date and amount (unique in these fixtures)."""
    root = parse_xml(document)
    return {
        f"{n.findtext('c:BookgDt/c:Dt', namespaces=NS)} {n.findtext('c:Amt', namespaces=NS)}":
            n.findtext("c:NtryRef", namespaces=NS)
        for n in root.findall(".//c:Ntry", NS)
    }


def test_camt_entry_after_the_window_end_extends_the_period(caplog) -> None:
    caplog.set_level("INFO", logger="fints2camt.fetch")
    source = FakeSource(camt_docs=[camt_with_forward_entry()])
    result = fetch_statement(source, ACCOUNT, WINDOW, created=CREATED)
    camt.validate_camt053(result.document)
    assert result.entry_count == 3
    assert period(result.document) == ("2026-10-21T00:00:00", "2026-10-27T23:59:59")
    assert balances(result.document)["CLBD"] == ("1200.01", "CRDT", "2026-10-27")
    assert result.window == WINDOW
    # the identifiers stay keyed on the window, so a re-run of the window stays idempotent
    plain = fetch_statement(FakeSource(camt_docs=[fixture_bytes("camt052_v02_synthetic.xml")]), ACCOUNT, WINDOW)
    msg_id = lambda document: parse_xml(document).findtext(".//c:GrpHdr/c:MsgId", namespaces=NS)
    assert msg_id(result.document) == msg_id(plain.document)
    messages = [record.getMessage() for record in caplog.records if record.name == "fints2camt.fetch"]
    assert messages == ["DE89…3000: 1 booked entries after 2026-10-25; statement period extended to 2026-10-27"]
    assert OWN_IBAN not in caplog.text and "6.00" not in caplog.text


def test_mt940_entry_after_the_window_end_extends_the_period() -> None:
    text = fixture_text("mt940_synthetic.sta") + "\n" + FORWARD_MT940
    result = fetch_statement(FakeSource(mt940_text=text), ACCOUNT, WINDOW, created=CREATED)
    camt.validate_camt053(result.document)
    assert result.entry_count == 6
    assert period(result.document) == ("2026-10-21T00:00:00", "2026-10-27T23:59:59")
    assert balances(result.document) == {
        "OPBD": ("1000.00", "CRDT", "2026-10-21"),
        "CLBD": ("939.00", "CRDT", "2026-10-27"),
    }


def test_without_later_entries_the_period_is_the_window(caplog) -> None:
    caplog.set_level("INFO", logger="fints2camt.fetch")
    result = fetch_statement(FakeSource(mt940_text=fixture_text("mt940_synthetic.sta")), ACCOUNT, WINDOW)
    assert period(result.document) == ("2026-10-21T00:00:00", "2026-10-25T23:59:59")
    assert not [record for record in caplog.records if record.name == "fints2camt.fetch"]


@pytest.mark.parametrize("source_format", ["camt", "mt940"])
def test_an_entry_after_the_window_end_keeps_its_reference_on_the_next_run(source_format: str) -> None:
    def source() -> FakeSource:
        if source_format == "camt":
            return FakeSource(camt_docs=[camt_with_forward_entry()])
        return FakeSource(mt940_text=fixture_text("mt940_synthetic.sta") + "\n" + FORWARD_MT940)

    # the run of 25.10. and the run of the next day (its window starts again after the overlap)
    next_day = FetchWindow(datetime.date(2026, 10, 22), datetime.date(2026, 10, 26))
    first = entry_references(fetch_statement(source(), ACCOUNT, WINDOW).document)
    second = entry_references(fetch_statement(source(), ACCOUNT, next_day).document)
    assert "2026-10-27 6.00" in first
    assert first == second
