"""The format-neutral statement model the converters fill and the camt.053 writer renders.

Amounts are always positive ``Decimal`` values; the direction is carried separately
(``credit``), as in camt. Balances are signed (a negative balance is a debit balance).
"""

from __future__ import annotations

import datetime
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class FetchWindow:
    """The booking-date range of one fetch, both ends inclusive."""

    start: datetime.date
    end: datetime.date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"window start {self.start} is after its end {self.end}")


@dataclass(frozen=True)
class Balance:
    """A booked balance at the end of ``date``; negative amounts are debit balances."""

    amount: Decimal
    currency: str
    date: datetime.date


@dataclass
class Party:
    """A counterparty: name, account (IBAN or another account number) and its bank."""

    name: str | None = None
    iban: str | None = None
    other_account: str | None = None
    bic: str | None = None
    bank_code: str | None = None

    def has_party_data(self) -> bool:
        return bool(self.name)

    def has_account_data(self) -> bool:
        return bool(self.iban or self.other_account)

    def has_agent_data(self) -> bool:
        return bool(self.bic or self.bank_code)


@dataclass
class TransactionDetails:
    """One underlying transaction of an entry (camt ``TxDtls``)."""

    end_to_end_id: str | None = None
    mandate_id: str | None = None
    account_servicer_reference: str | None = None
    debtor: Party | None = None
    creditor: Party | None = None
    creditor_id: str | None = None
    ultimate_debtor: str | None = None
    ultimate_creditor: str | None = None
    remittance: list[str] = field(default_factory=list)


@dataclass
class BankTransactionCode:
    """ISO domain code (``Domn``) and/or a proprietary code (``Prtry``)."""

    domain: str | None = None
    family: str | None = None
    sub_family: str | None = None
    proprietary: str | None = None
    proprietary_issuer: str | None = None


@dataclass
class Entry:
    """One booked entry of a statement (camt ``Ntry``)."""

    amount: Decimal
    currency: str
    credit: bool
    booking_date: datetime.date
    value_date: datetime.date | None = None
    reversal: bool = False
    account_servicer_reference: str | None = None
    entry_reference: str | None = None
    bank_transaction_code: BankTransactionCode = field(default_factory=BankTransactionCode)
    batch: bool = False
    batch_count: int | None = None
    transactions: list[TransactionDetails] = field(default_factory=list)
    additional_info: str | None = None

    def signed_amount(self) -> Decimal:
        return self.amount if self.credit else -self.amount

    def fingerprint(self, iban: str) -> str:
        """The content of the entry that identifies it, independent of the fetch window."""
        parts: list[str] = [
            iban,
            self.booking_date.isoformat(),
            self.value_date.isoformat() if self.value_date else "",
            "C" if self.credit else "D",
            "R" if self.reversal else "",
            f"{self.amount:f}",
            self.currency,
            self.account_servicer_reference or "",
            self.bank_transaction_code.proprietary or "",
        ]
        for tx in self.transactions:
            for party in (tx.debtor, tx.creditor):
                parts += [party.name or "", party.iban or "", party.other_account or ""] if party else ["", "", ""]
            parts += [tx.end_to_end_id or "", tx.mandate_id or "", tx.creditor_id or "", *tx.remittance]
        return "\x1f".join(parts)


@dataclass
class Statement:
    """The statement of one account over one fetch window."""

    iban: str
    currency: str
    window: FetchWindow
    opening: Balance
    closing: Balance
    entries: list[Entry]
    bic: str | None = None


def assign_entry_references(iban: str, entries: Iterable[Entry]) -> None:
    """Give every entry without one a deterministic reference (camt ``NtryRef``).

    The reference is a hash of the entry content plus the occurrence number among
    identical entries, so a re-fetch of an overlapping window yields the same reference
    for the same booking and targets can deduplicate on it.
    """
    seen: dict[str, int] = {}
    for entry in entries:
        if entry.entry_reference:
            continue
        fingerprint = entry.fingerprint(iban)
        occurrence = seen.get(fingerprint, 0)
        seen[fingerprint] = occurrence + 1
        entry.entry_reference = deterministic_reference(f"{fingerprint}\x1e{occurrence}")


def deterministic_reference(content: str) -> str:
    """A 32-character reference derived from ``content`` (fits camt's Max35Text)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
