"""Fetch one account's transactions from the bank and produce its camt.053 statement."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from lxml import etree

from . import camt, mt940
from .config import AccountConfig, FetchFormat
from .errors import BankError, CamtError
from .fints_source import BankSource
from .model import Balance, Entry, FetchWindow, Statement, assign_entry_references


@dataclass(frozen=True)
class FetchResult:
    iban: str
    window: FetchWindow
    document: bytes
    entry_count: int
    source_format: Literal["camt", "mt940"]


def fetch_statement(
    source: BankSource,
    account: AccountConfig,
    window: FetchWindow,
    *,
    fetch_format: FetchFormat = "auto",
    bank_bic: str | None = None,
    created: datetime.datetime | None = None,
) -> FetchResult:
    """Fetch ``window`` of ``account`` and return a validated camt.053.001.02 document.

    camt via HKCAZ is used when the bank offers it (``auto``) and passed through,
    otherwise MT940 via HKKAZ is converted. Balances come from the bank's statement data
    when it has them, else from HKSAL (closing) and the entries (opening).
    """
    use_camt = fetch_format == "camt" or (fetch_format == "auto" and source.supports_camt())
    if fetch_format == "camt" and not source.supports_camt():
        raise BankError("the bank does not offer camt (HKCAZ); set fetch.format to auto or mt940")

    raw_entries: list[etree._Element] = []
    if use_camt:
        bank_camt = camt.read_bank_camt(source.fetch_camt(account.iban, window), account.iban)
        entries = bank_camt.entries
        raw_entries = bank_camt.raw_entries
        opening, closing = bank_camt.opening, bank_camt.closing
        currency = bank_camt.currency or account.currency
    else:
        statements = mt940.parse(source.fetch_mt940(account.iban, window))
        entries = mt940.to_entries(statements, account.currency)
        opening = statements[0].opening if statements else None
        closing = statements[-1].closing if statements else None
        currency = opening.currency if opening else account.currency

    entries.sort(key=lambda entry: entry.booking_date)
    assign_entry_references(account.iban, entries)
    net = _net(entries) + sum(
        (_raw_signed_amount(element) for element in raw_entries), Decimal(0)
    )
    opening, closing = _balances(opening, closing, net, lambda: source.fetch_balance(account.iban))

    statement = Statement(
        iban=account.iban,
        currency=currency,
        window=window,
        opening=replace(opening, currency=opening.currency or currency, date=window.start),
        closing=replace(closing, currency=closing.currency or currency, date=window.end),
        entries=entries,
        bic=bank_bic,
    )
    document = camt.render_camt053(
        statement,
        created or datetime.datetime.now(datetime.timezone.utc),
        raw_entries=raw_entries,
    )
    camt.validate_camt053(document)
    return FetchResult(
        iban=account.iban,
        window=window,
        document=document,
        entry_count=len(entries) + len(raw_entries),
        source_format="camt" if use_camt else "mt940",
    )


def _net(entries: list[Entry]) -> Decimal:
    return sum((entry.signed_amount() for entry in entries), Decimal(0))


def _raw_signed_amount(element: etree._Element) -> Decimal:
    amount, credit = camt.raw_entry_movement(element)
    return amount if credit else -amount


def _balances(
    opening: Balance | None,
    closing: Balance | None,
    net: Decimal,
    fetch_balance: Callable[[], Balance | None],
) -> tuple[Balance, Balance]:
    """Complete opening and closing balance: one is derived from the other and the entries."""
    if opening is not None and closing is not None:
        return opening, closing
    if opening is not None:
        return opening, replace(opening, amount=opening.amount + net)
    if closing is None:
        closing = fetch_balance()
        if closing is None:
            raise CamtError("no balance available: neither the statement data nor HKSAL provide one")
    return replace(closing, amount=closing.amount - net), closing
