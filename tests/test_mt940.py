import datetime
from decimal import Decimal

import pytest

from fints2camt import mt940
from fints2camt.errors import Mt940Error

from conftest import fixture_text


def entries():
    return mt940.to_entries(mt940.parse(fixture_text("mt940_synthetic.sta")), "EUR")


def test_parse_statements_and_balances(mt940_text: str) -> None:
    statements = mt940.parse(mt940_text)
    assert len(statements) == 2
    assert statements[0].account == "37040044/0532013000"
    assert statements[0].opening.amount == Decimal("1000.00")
    assert statements[0].opening.date == datetime.date(2026, 10, 22)
    assert statements[1].closing.amount == Decimal("945.00")
    assert [len(s.transactions) for s in statements] == [2, 3]
    assert all(isinstance(t.amount, Decimal) for s in statements for t in s.transactions)


def test_incoming_credit_transfer() -> None:
    entry = entries()[0]
    assert entry.credit and not entry.reversal
    assert entry.amount == Decimal("250.00")
    assert entry.booking_date == entry.value_date == datetime.date(2026, 10, 23)
    assert entry.account_servicer_reference == "9000000001"
    assert entry.bank_transaction_code.proprietary == "NTRF+166+000"
    assert entry.bank_transaction_code.proprietary_issuer == "DK"
    assert (entry.bank_transaction_code.domain, entry.bank_transaction_code.family,
            entry.bank_transaction_code.sub_family) == ("PMNT", "RCDT", "ESCT")
    assert entry.additional_info == "SEPA-UEBERWEISUNG"
    (tx,) = entry.transactions
    assert tx.creditor is None
    assert tx.debtor.name == "Muster GmbH"
    assert tx.debtor.iban == "DE42100000000001234567"
    assert tx.debtor.bic == "COBADEFFXXX"
    assert tx.end_to_end_id == "INV-2026-0042"
    assert tx.ultimate_debtor == "Beispiel Holding AG"
    # the purpose spans two subfields and an MT940 line break
    assert tx.remittance == ["Rechnung 2026-0042 Kundennr 4711"]


def test_direct_debit() -> None:
    entry = entries()[1]
    assert not entry.credit and not entry.reversal
    assert entry.amount == Decimal("49.99")
    assert entry.bank_transaction_code.proprietary == "NDDT+105+992"
    assert entry.bank_transaction_code.sub_family == "ESDD"
    (tx,) = entry.transactions
    assert tx.debtor is None
    assert tx.creditor.name == "Beispiel Versicherung VVaG"
    assert tx.creditor.iban == "DE37200000000009876543"
    assert tx.creditor.bic == "GENODEF1XXX"
    assert tx.mandate_id == "M-000123"
    assert tx.creditor_id == "DE98ZZZ09999999999"
    assert tx.end_to_end_id == "DD-77-2026-10"
    assert tx.remittance == ["Beitrag Oktober 2026"]


def test_batch_entry() -> None:
    entry = entries()[2]
    assert entry.batch
    assert entry.batch_count == 3
    assert not entry.credit
    assert entry.amount == Decimal("300.00")
    (tx,) = entry.transactions
    # a German account number and bank code instead of IBAN and BIC
    assert tx.creditor.other_account == "0532013000"
    assert tx.creditor.bank_code == "37040044"
    assert tx.creditor.name == "Fictitious Payroll Batch"


def test_reversal_of_direct_debit_keeps_party_roles() -> None:
    entry = entries()[3]
    assert entry.reversal
    assert entry.credit  # RD: reversal of a debit is booked as a credit
    assert entry.amount == Decimal("49.99")
    (tx,) = entry.transactions
    assert tx.creditor.name == "Beispiel Versicherung VVaG"
    assert tx.debtor is None
    assert tx.mandate_id == "M-000123"
    assert tx.remittance == ["Rueckgabe AC04 Konto erloschen"]


def test_unstructured_details_and_nonref() -> None:
    entry = entries()[4]
    assert entry.account_servicer_reference is None
    assert entry.bank_transaction_code.proprietary == "NCHG"
    assert entry.bank_transaction_code.domain is None
    assert entry.transactions[0].remittance == ["Kontofuehrungsgebuehr Oktober 2026"]
    assert entry.transactions[0].debtor is None and entry.transactions[0].creditor is None


def test_reversal_of_credit_is_a_debit() -> None:
    data = ":20:X\n:60F:C261001EUR10,00\n:61:2610011001RC10,00NTRFNONREF\n:86:Storno\n:62F:C261001EUR0,00\n-"
    (entry,) = mt940.to_entries(mt940.parse(data), "EUR")
    assert entry.reversal and not entry.credit
    # the original was a credit, so the counterparty stays the debtor
    assert entry.transactions[0].remittance == ["Storno"]


@pytest.mark.parametrize(
    ("line", "booking"),
    [
        ("2512310102D1,00NMSCNONREF", datetime.date(2026, 1, 2)),
        ("2601021231D1,00NMSCNONREF", datetime.date(2025, 12, 31)),
        ("261024D1,00NMSCNONREF", datetime.date(2026, 10, 24)),
    ],
)
def test_booking_date_year_inference(line: str, booking: datetime.date) -> None:
    (statement,) = mt940.parse(f":20:X\n:61:{line}\n:86:x\n-")
    assert statement.transactions[0].booking_date == booking


def test_amount_with_funds_code_and_supplementary_details() -> None:
    (statement,) = mt940.parse(":20:X\n:61:2610241024CR1234,5NTRFKREF1//BANK1\nSUPPL DETAILS\n:86:x\n-")
    transaction = statement.transactions[0]
    assert transaction.amount == Decimal("1234.50")
    assert transaction.customer_reference == "KREF1"
    assert transaction.bank_reference == "BANK1"
    assert transaction.supplementary == "SUPPL DETAILS"


def test_invalid_61_raises() -> None:
    with pytest.raises(Mt940Error):
        mt940.parse(":20:X\n:61:not a transaction\n-")


def test_parse_86_non_sepa_purpose_lines_keep_word_breaks() -> None:
    details = mt940.parse_86("835?00ENTGELT?20Zinsen 3. Quartal?21laut Abrechnung")
    assert details.gvc == "835"
    assert details.purpose == "Zinsen 3. Quartal laut Abrechnung"
    assert details.keywords == {}
