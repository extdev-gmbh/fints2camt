import datetime
from decimal import Decimal

import pytest
from lxml import etree

from fints2camt import camt
from fints2camt.errors import CamtError, CamtValidationError
from fints2camt.model import Balance, Entry, FetchWindow, Party, Statement, TransactionDetails

from conftest import NS, OWN_IBAN, fixture_bytes, parse_xml

CREATED = datetime.datetime(2026, 10, 25, 6, 0, tzinfo=datetime.timezone.utc)
WINDOW = FetchWindow(datetime.date(2026, 10, 22), datetime.date(2026, 10, 25))


def statement(entries: list[Entry]) -> Statement:
    return Statement(
        iban=OWN_IBAN,
        currency="EUR",
        window=WINDOW,
        opening=Balance(Decimal("-10.50"), "EUR", WINDOW.start),
        closing=Balance(Decimal("100.00"), "EUR", WINDOW.end),
        entries=entries,
        bic="COBADEFFXXX",
    )


def test_minimal_statement_validates_and_has_balances() -> None:
    document = camt.render_camt053(statement([]), CREATED)
    camt.validate_camt053(document)
    root = parse_xml(document)
    balances = root.findall(".//c:Stmt/c:Bal", NS)
    assert [b.findtext("c:Tp/c:CdOrPrtry/c:Cd", namespaces=NS) for b in balances] == ["OPBD", "CLBD"]
    assert balances[0].findtext("c:Amt", namespaces=NS) == "10.50"
    assert balances[0].findtext("c:CdtDbtInd", namespaces=NS) == "DBIT"
    assert balances[1].findtext("c:Dt/c:Dt", namespaces=NS) == "2026-10-25"
    assert root.findtext(".//c:Stmt/c:Acct/c:Id/c:IBAN", namespaces=NS) == OWN_IBAN
    assert root.findtext(".//c:Stmt/c:FrToDt/c:FrDtTm", namespaces=NS) == "2026-10-22T00:00:00"


def test_statement_id_is_deterministic_per_account_and_window() -> None:
    first = parse_xml(camt.render_camt053(statement([]), CREATED))
    second = parse_xml(camt.render_camt053(statement([]), CREATED + datetime.timedelta(days=1)))
    assert first.findtext(".//c:Stmt/c:Id", namespaces=NS) == second.findtext(".//c:Stmt/c:Id", namespaces=NS)


def test_long_and_odd_texts_are_cleaned_to_fit_the_schema() -> None:
    entry = Entry(
        amount=Decimal("1"),
        currency="EUR",
        credit=True,
        booking_date=WINDOW.start,
        entry_reference="r1",
        transactions=[
            TransactionDetails(
                debtor=Party(name="N" * 100 + "\x01", iban="not-an-iban", bic="nobic", bank_code=None),
                end_to_end_id="E" * 50,
                remittance=["x" * 300],
            )
        ],
        additional_info="i" * 600,
    )
    document = camt.render_camt053(statement([entry]), CREATED)
    camt.validate_camt053(document)
    root = parse_xml(document)
    assert len(root.findtext(".//c:Dbtr/c:Nm", namespaces=NS)) == 70
    assert root.findtext(".//c:DbtrAcct/c:Id/c:Othr/c:Id", namespaces=NS) == "not-an-iban"
    assert [len(u.text) for u in root.findall(".//c:Ustrd", NS)] == [140, 140, 20]


def test_validation_rejects_an_invalid_document() -> None:
    document = camt.render_camt053(statement([]), CREATED)
    broken = document.replace(b"<Ccy>EUR</Ccy>", b"<Ccy>EURO</Ccy>")
    with pytest.raises(CamtValidationError):
        camt.validate_camt053(broken)
    with pytest.raises(CamtValidationError):
        camt.validate_camt053(b"<not-closed>")


def test_validation_errors_name_line_element_and_facet_never_the_value() -> None:
    document = camt.render_camt053(statement([]), CREATED)
    broken = document.replace(b"<Ccy>EUR</Ccy>", b"<Ccy>Synthetic Name</Ccy>", 1)
    with pytest.raises(CamtValidationError) as excinfo:
        camt.validate_camt053(broken)
    message = str(excinfo.value)
    assert "element Ccy" in message and "facet pattern" in message and "line " in message
    assert "Synthetic" not in message
    with pytest.raises(CamtValidationError) as excinfo:
        camt.validate_camt053(b"<a>\n<Synthetic-Name></a>")
    assert "Synthetic" not in str(excinfo.value)


def test_v02_pass_through_keeps_booked_entries_verbatim() -> None:
    source = fixture_bytes("camt052_v02_synthetic.xml")
    bank = camt.read_bank_camt([source], OWN_IBAN)
    assert bank.entries == []
    assert len(bank.raw_entries) == 2  # the pending entry is dropped
    assert bank.opening == Balance(Decimal("1000.00"), "EUR", datetime.date(2026, 10, 22))
    assert bank.closing.amount == Decimal("1200.01")
    assert bank.currency == "EUR"

    document = camt.render_camt053(
        statement([]), CREATED, raw_entries=bank.raw_entries
    )
    camt.validate_camt053(document)
    root = parse_xml(document)
    ntries = root.findall(".//c:Ntry", NS)
    assert len(ntries) == 2
    first = ntries[0]
    assert len(first.findtext("c:NtryRef", namespaces=NS)) == 32
    # everything except the added NtryRef is the bank's element, moved into camt.053
    original = etree.fromstring(source).findall(".//{*}Ntry")[0]
    assert [etree.QName(e).localname for e in first.iter()][2:] == [
        etree.QName(e).localname for e in original.iter()
    ][1:]
    assert first.findtext(".//c:RmtInf/c:Ustrd", namespaces=NS) == "Rechnung 2026-0042 Kundennr 4711"
    assert first.findtext(".//c:BkTxCd/c:Prtry/c:Cd", namespaces=NS) == "NTRF+166+000"
    assert root.findtext(".//c:TxsSummry/c:TtlNtries/c:NbOfNtries", namespaces=NS) == "2"
    assert root.findtext(".//c:TxsSummry/c:TtlNtries/c:TtlNetNtryAmt", namespaces=NS) == "200.01"


def test_pass_through_references_are_stable_across_fetches() -> None:
    source = fixture_bytes("camt052_v02_synthetic.xml")
    first = camt.read_bank_camt([source], OWN_IBAN).raw_entries
    second = camt.read_bank_camt([source], OWN_IBAN).raw_entries
    refs = [e.findtext("c:NtryRef", namespaces=NS) for e in first]
    assert refs == [e.findtext("c:NtryRef", namespaces=NS) for e in second]
    assert len(set(refs)) == 2


def test_other_account_reports_are_ignored() -> None:
    source = fixture_bytes("camt052_v02_synthetic.xml")
    bank = camt.read_bank_camt([source], "DE02120300000000202051")
    assert bank.raw_entries == [] and bank.opening is None and bank.closing is None


def test_v08_is_mapped_through_the_model() -> None:
    bank = camt.read_bank_camt([fixture_bytes("camt052_v08_synthetic.xml")], OWN_IBAN)
    assert bank.raw_entries == []
    batch, reversal = bank.entries
    assert batch.batch and batch.batch_count == 2
    assert batch.entry_reference == "BANK-REF-1"
    assert [tx.creditor.name for tx in batch.transactions] == ["Erika Mustermann", "Max Mustermann"]
    assert batch.transactions[0].creditor.bic == "DEUTDEFFXXX"
    assert batch.transactions[0].creditor.iban == "DE04300000000005555555"
    assert (batch.bank_transaction_code.domain, batch.bank_transaction_code.proprietary) == ("PMNT", None)
    assert reversal.reversal and reversal.credit
    assert reversal.booking_date == datetime.date(2026, 10, 24)
    assert reversal.transactions[0].creditor_id == "DE98ZZZ09999999999"
    assert reversal.transactions[0].mandate_id == "M-000123"

    document = camt.render_camt053(statement(bank.entries), CREATED)
    camt.validate_camt053(document)
    root = parse_xml(document)
    assert root.findtext(".//c:Btch/c:NbOfTxs", namespaces=NS) == "2"
    assert len(root.findall(".//c:Ntry[1]/c:NtryDtls/c:TxDtls", NS)) == 2
    assert root.findtext(".//c:Ntry[2]/c:RvslInd", namespaces=NS) == "true"
    assert root.findtext(
        ".//c:Ntry[2]//c:Cdtr/c:Id/c:PrvtId/c:Othr/c:Id", namespaces=NS
    ) == "DE98ZZZ09999999999"


def test_unsupported_or_broken_bank_documents_raise() -> None:
    with pytest.raises(CamtError):
        camt.read_bank_camt([b"<Document xmlns='urn:example'/>"], OWN_IBAN)
    with pytest.raises(CamtError):
        camt.read_bank_camt([b"<Document"], OWN_IBAN)


def test_external_entities_are_not_resolved(tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET")
    document = (
        f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file://{secret}">]>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.052.001.08"><BkToCstmrAcctRpt><Rpt>'
        f'<Acct><Id><IBAN>{OWN_IBAN}</IBAN></Id><Ccy>EUR</Ccy></Acct>'
        '<Ntry><Amt Ccy="EUR">1.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>'
        '<BookgDt><Dt>2026-10-24</Dt></BookgDt><AddtlNtryInf>&x;</AddtlNtryInf></Ntry>'
        '</Rpt></BkToCstmrAcctRpt></Document>'
    ).encode()
    bank = camt.read_bank_camt([document], OWN_IBAN)
    assert "SECRET" not in (bank.entries[0].additional_info or "")
