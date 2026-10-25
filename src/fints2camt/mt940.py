"""MT940 (as delivered by German banks for HKKAZ) to the statement model.

Covers the German flavour of MT940 ("DFÜ-Abkommen", Anlage 3): the structured ``:86:``
field with the business transaction code (GVC) and the ``?nn`` subfields, and the SEPA
keywords inside the purpose (``EREF+``, ``MREF+``, ``CRED+``, ``SVWZ+``, ``ABWA+`` …).
Unstructured ``:86:`` fields are taken as plain remittance text.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .errors import Mt940Error
from .model import Balance, BankTransactionCode, Entry, Party, TransactionDetails

_TAG = re.compile(r"^:(\d{2}[A-Z]?):(.*)$")
_BALANCE = re.compile(r"^(?P<mark>[CD])(?P<date>\d{6})(?P<currency>[A-Z]{3})(?P<amount>\d+,\d*)")
_LINE_61 = re.compile(
    r"^(?P<value_date>\d{6})(?P<entry_date>\d{4})?"
    r"(?P<mark>RC|RD|C|D)(?P<funds>[A-Z])?"
    r"(?P<amount>\d+,\d*)"
    r"(?P<type>[NFS][A-Z0-9]{3})"
    r"(?P<customer_ref>.*?)"
    r"(?://(?P<bank_ref>.*))?$"
)
_SUBFIELD = re.compile(r"\?(\d{2})")
_SEPA_KEYWORDS = ("EREF", "KREF", "MREF", "CRED", "DEBT", "COAM", "OAMT", "SVWZ", "ABWA", "ABWE", "IBAN", "BIC")
_SEPA_KEYWORD = re.compile(r"(" + "|".join(_SEPA_KEYWORDS) + r")\+")
_IBAN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Za-z0-9]{1,30}$")
_BIC = re.compile(r"^[A-Z]{6}[A-Z2-9][A-NP-Z0-9]([A-Z0-9]{3})?$")
_BLZ = re.compile(r"^\d{8}$")
_BATCH_COUNT = re.compile(r"(\d+)\s+POSTEN|ANZ(?:AHL|\.)?\s*:?\s*(\d+)", re.IGNORECASE)

# GVCs of collective bookings ("Sammler"): the entry stands for several transactions.
BATCH_GVCS = frozenset({"191", "192"})

# A small, conservative map of SEPA GVCs to ISO bank transaction codes. GVCs not listed
# here keep only the proprietary DK code, which every entry carries.
GVC_DOMAIN_CODES: dict[str, tuple[str, str, str]] = {
    "104": ("PMNT", "IDDT", "BBDD"),  # SEPA B2B direct debit, debited
    "105": ("PMNT", "IDDT", "ESDD"),  # SEPA core direct debit, debited
    "116": ("PMNT", "ICDT", "ESCT"),  # SEPA credit transfer, outgoing
    "166": ("PMNT", "RCDT", "ESCT"),  # SEPA credit transfer, incoming
    "171": ("PMNT", "RDDT", "ESDD"),  # SEPA core direct debit collection, credited
    "174": ("PMNT", "RDDT", "BBDD"),  # SEPA B2B direct debit collection, credited
    "177": ("PMNT", "ICDT", "ESCT"),  # SEPA credit transfer (online banking), outgoing
}


@dataclass
class Mt940Transaction:
    """One ``:61:`` line with its ``:86:`` field, still in MT940 terms."""

    value_date: datetime.date
    booking_date: datetime.date
    mark: str
    amount: Decimal
    swift_code: str
    customer_reference: str | None
    bank_reference: str | None
    supplementary: str | None
    details: str | None = None


@dataclass
class Mt940Statement:
    """One MT940 message (``:20:`` … ``:62F:``)."""

    account: str | None = None
    opening: Balance | None = None
    closing: Balance | None = None
    transactions: list[Mt940Transaction] = field(default_factory=list)


@dataclass
class Details86:
    """The decoded ``:86:`` field."""

    gvc: str | None = None
    posting_text: str | None = None
    purpose: str = ""
    bank: str | None = None
    account: str | None = None
    name: str | None = None
    gvc_extension: str | None = None
    keywords: dict[str, str] = field(default_factory=dict)


def parse(data: str) -> list[Mt940Statement]:
    """Parse the concatenated MT940 messages of one HKKAZ response."""
    statements: list[Mt940Statement] = []
    for tag, value in _tags(data):
        if tag == "20" or not statements:
            statements.append(Mt940Statement())
            if tag == "20":
                continue
        current = statements[-1]
        if tag == "25":
            current.account = value.strip()
        elif tag in ("60F", "60M"):
            current.opening = _parse_balance(value, tag)
        elif tag in ("62F", "62M"):
            current.closing = _parse_balance(value, tag)
        elif tag == "61":
            current.transactions.append(_parse_61(value))
        elif tag == "86" and current.transactions and current.transactions[-1].details is None:
            current.transactions[-1].details = value
    return [s for s in statements if s.transactions or s.opening or s.closing]


def _tags(data: str) -> list[tuple[str, str]]:
    """Split MT940 text into ``(tag, value)`` pairs; continuation lines keep their newline."""
    tags: list[tuple[str, list[str]]] = []
    for raw_line in data.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if not line:
            continue
        if line == "-" or line.startswith("-}") or line.startswith("{"):
            continue
        match = _TAG.match(line)
        if match:
            tags.append((match.group(1), [match.group(2)]))
        elif tags:
            tags[-1][1].append(line)
        else:
            raise Mt940Error("MT940 data does not start with a tag")
    return [(tag, "\n".join(lines)) for tag, lines in tags]


def _amount(text: str) -> Decimal:
    integer, _, fraction = text.partition(",")
    try:
        return Decimal(f"{integer}.{fraction or '0'}").quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise Mt940Error(f"invalid MT940 amount {text!r}") from exc


def _yymmdd(text: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(text, "%y%m%d").date()
    except ValueError as exc:
        raise Mt940Error(f"invalid MT940 date {text!r}") from exc


def _parse_balance(value: str, tag: str) -> Balance:
    match = _BALANCE.match(value.strip())
    if not match:
        raise Mt940Error(f"invalid MT940 balance in :{tag}:")
    amount = _amount(match.group("amount"))
    return Balance(
        amount=-amount if match.group("mark") == "D" else amount,
        currency=match.group("currency"),
        date=_yymmdd(match.group("date")),
    )


def _parse_61(value: str) -> Mt940Transaction:
    first, _, supplementary = value.partition("\n")
    match = _LINE_61.match(first.strip())
    if not match:
        raise Mt940Error("invalid MT940 :61: line")
    value_date = _yymmdd(match.group("value_date"))
    booking_date = value_date
    if match.group("entry_date"):
        booking_date = _entry_date(value_date, match.group("entry_date"))
    customer_ref = (match.group("customer_ref") or "").strip() or None
    bank_ref = (match.group("bank_ref") or "").strip() or None
    return Mt940Transaction(
        value_date=value_date,
        booking_date=booking_date,
        mark=match.group("mark"),
        amount=_amount(match.group("amount")),
        swift_code=match.group("type"),
        customer_reference=customer_ref,
        bank_reference=bank_ref,
        supplementary=supplementary.replace("\n", "").strip() or None,
    )


def _entry_date(value_date: datetime.date, mmdd: str) -> datetime.date:
    """The booking date (MMDD) gets the year that puts it closest to the value date."""
    month, day = int(mmdd[:2]), int(mmdd[2:])
    year = value_date.year
    if month - value_date.month > 6:
        year -= 1
    elif value_date.month - month > 6:
        year += 1
    try:
        return datetime.date(year, month, day)
    except ValueError as exc:
        raise Mt940Error(f"invalid MT940 entry date {mmdd!r}") from exc


def parse_86(text: str) -> Details86:
    """Decode a ``:86:`` field (structured with ``?nn`` subfields, or free text)."""
    joined = text.replace("\n", "")
    if not re.match(r"^\d{3}\?", joined):
        return Details86(purpose=" ".join(line.strip() for line in text.split("\n")).strip())
    details = Details86(gvc=joined[:3])
    purpose_lines: list[str] = []
    names: list[str] = []
    parts = _SUBFIELD.split(joined[3:])
    # parts: ['', '00', 'text', '10', 'text', ...]
    for code, content in zip(parts[1::2], parts[2::2]):
        if code == "00":
            details.posting_text = content.strip() or None
        elif "20" <= code <= "29" or "60" <= code <= "63":
            purpose_lines.append(content)
        elif code == "30":
            details.bank = content.strip() or None
        elif code == "31":
            details.account = content.strip() or None
        elif code in ("32", "33"):
            names.append(content)
        elif code == "34":
            details.gvc_extension = content.strip() or None
    details.name = "".join(names).strip() or None
    compact = "".join(purpose_lines)
    if _SEPA_KEYWORD.match(compact):
        details.keywords = _split_keywords(compact)
        details.purpose = details.keywords.get("SVWZ", "")
    else:
        details.purpose = " ".join(line.strip() for line in purpose_lines if line.strip())
    return details


def _split_keywords(text: str) -> dict[str, str]:
    matches = list(_SEPA_KEYWORD.finditer(text))
    keywords: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        if value and match.group(1) not in keywords:
            keywords[match.group(1)] = value
    return keywords


def to_entries(statements: list[Mt940Statement], currency: str) -> list[Entry]:
    """Map the parsed transactions to statement entries."""
    entries: list[Entry] = []
    for statement in statements:
        statement_currency = statement.opening.currency if statement.opening else currency
        for transaction in statement.transactions:
            entries.append(_to_entry(transaction, statement_currency))
    return entries


def _to_entry(transaction: Mt940Transaction, currency: str) -> Entry:
    details = parse_86(transaction.details) if transaction.details else Details86()
    reversal = transaction.mark in ("RC", "RD")
    # RC (reversal of a credit) is booked as a debit, RD (reversal of a debit) as a credit.
    credit = transaction.mark in ("C", "RD")
    # The parties keep the roles of the original transaction.
    original_credit = credit != reversal

    counterparty = _counterparty(details)
    keywords = details.keywords
    tx = TransactionDetails(
        end_to_end_id=keywords.get("EREF"),
        mandate_id=keywords.get("MREF"),
        creditor_id=keywords.get("CRED"),
        ultimate_debtor=keywords.get("ABWA"),
        ultimate_creditor=keywords.get("ABWE"),
        remittance=[details.purpose] if details.purpose else [],
    )
    if original_credit:
        tx.debtor = counterparty
    else:
        tx.creditor = counterparty

    batch = details.gvc in BATCH_GVCS or "SAMMLER" in (details.posting_text or "").upper()
    batch_count = None
    if batch:
        count_match = _BATCH_COUNT.search(details.purpose)
        if count_match:
            batch_count = int(count_match.group(1) or count_match.group(2))

    bank_reference = transaction.bank_reference
    if bank_reference and bank_reference.upper() == "NONREF":
        bank_reference = None

    return Entry(
        amount=transaction.amount,
        currency=currency,
        credit=credit,
        booking_date=transaction.booking_date,
        value_date=transaction.value_date,
        reversal=reversal,
        account_servicer_reference=bank_reference,
        bank_transaction_code=_bank_transaction_code(transaction.swift_code, details),
        batch=batch,
        batch_count=batch_count,
        transactions=[tx],
        additional_info=details.posting_text,
    )


def _counterparty(details: Details86) -> Party | None:
    party = Party(name=details.name)
    account = (details.account or "").replace(" ", "")
    if account and _IBAN.match(account.upper()):
        party.iban = account.upper()
    elif account and account.strip("0"):
        party.other_account = account
    bank = (details.bank or "").replace(" ", "").upper()
    if _BIC.match(bank):
        party.bic = bank
    elif _BLZ.match(bank):
        party.bank_code = bank
    if party.has_party_data() or party.has_account_data() or party.has_agent_data():
        return party
    return None


def _bank_transaction_code(swift_code: str, details: Details86) -> BankTransactionCode:
    proprietary = swift_code
    if details.gvc:
        proprietary += f"+{details.gvc}"
        if details.gvc_extension:
            proprietary += f"+{details.gvc_extension}"
    code = BankTransactionCode(proprietary=proprietary, proprietary_issuer="DK")
    domain = GVC_DOMAIN_CODES.get(details.gvc or "")
    if domain:
        code.domain, code.family, code.sub_family = domain
    return code
