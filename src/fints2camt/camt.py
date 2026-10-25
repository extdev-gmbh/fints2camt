"""camt.053.001.02: writing, XSD validation, and reading the camt documents banks send for HKCAZ.

Banks answer HKCAZ with camt.052 (or camt.053) documents, often one per day. They are
normalised into one camt.053.001.02 statement per account and fetch window:

* documents in a ``*.001.02`` version share the entry type (``ReportEntry2``) with
  camt.053.001.02, so their booked ``Ntry`` elements are passed through unchanged (only
  moved into the camt.053 namespace);
* documents in any other version are read into the statement model and written anew.
"""

from __future__ import annotations

import copy
import datetime
import functools
import importlib.resources
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from lxml import etree

from .errors import CamtError, CamtValidationError
from .model import (
    Balance,
    BankTransactionCode,
    Entry,
    Party,
    Statement,
    TransactionDetails,
    deterministic_reference,
)

CAMT053_NS = "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"
_CAMT_NS = re.compile(r"^urn:iso:std:iso:20022:tech:xsd:camt\.05[234]\.001\.(\d{2})$")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_IBAN = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Za-z0-9]{1,30}$")
_BIC = re.compile(r"^[A-Z]{6}[A-Z2-9][A-NP-Z0-9]([A-Z0-9]{3})?$")
_OPENING_CODES = ("OPBD", "PRCD")
_CLOSING_CODES = ("CLBD",)


# --- writing -------------------------------------------------------------------------------


def _q(tag: str) -> str:
    return f"{{{CAMT053_NS}}}{tag}"


def _sub(parent: etree._Element, tag: str, text: str | None = None, **attrib: str) -> etree._Element:
    element = etree.SubElement(parent, _q(tag), attrib)
    if text is not None:
        element.text = text
    return element


def _text(value: str | None, max_length: int) -> str | None:
    """Clean a text for a camt ``MaxNText`` element; ``None`` when nothing is left."""
    if value is None:
        return None
    cleaned = " ".join(_CONTROL_CHARS.sub(" ", value).split())
    return cleaned[:max_length] or None


def _format_amount(amount: Decimal) -> str:
    amount = abs(amount)
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent >= -2:
        amount = amount.quantize(Decimal("0.01"))
    return f"{amount:f}"


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def render_camt053(
    statement: Statement,
    created: datetime.datetime,
    raw_entries: Iterable[etree._Element] = (),
) -> bytes:
    """Render ``statement`` as a camt.053.001.02 document (UTF-8 bytes).

    ``raw_entries`` are ``Ntry`` elements already in the camt.053.001.02 namespace (the
    pass-through of a bank's camt); they follow the model entries of ``statement``.
    """
    raw = [copy.deepcopy(element) for element in raw_entries]
    reference = deterministic_reference(
        f"{statement.iban}|{statement.window.start.isoformat()}|{statement.window.end.isoformat()}"
    )
    created_text = created.replace(microsecond=0).isoformat()

    document = etree.Element(_q("Document"), nsmap={None: CAMT053_NS})
    message = _sub(document, "BkToCstmrStmt")
    header = _sub(message, "GrpHdr")
    _sub(header, "MsgId", reference)
    _sub(header, "CreDtTm", created_text)

    stmt = _sub(message, "Stmt")
    _sub(stmt, "Id", reference)
    _sub(stmt, "CreDtTm", created_text)
    period = _sub(stmt, "FrToDt")
    _sub(period, "FrDtTm", f"{statement.window.start.isoformat()}T00:00:00")
    _sub(period, "ToDtTm", f"{statement.window.end.isoformat()}T23:59:59")
    account = _sub(stmt, "Acct")
    _sub(_sub(account, "Id"), "IBAN", statement.iban)
    _sub(account, "Ccy", statement.currency)
    if statement.bic:
        _sub(_sub(_sub(account, "Svcr"), "FinInstnId"), "BIC", statement.bic)
    _balance(stmt, "OPBD", statement.opening)
    _balance(stmt, "CLBD", statement.closing)

    movements = [(entry.amount, entry.credit) for entry in statement.entries]
    movements += [raw_entry_movement(element) for element in raw]
    _summary(stmt, movements)

    for entry in statement.entries:
        _entry(stmt, entry)
    for element in raw:
        stmt.append(element)

    return etree.tostring(document, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _balance(stmt: etree._Element, code: str, balance: Balance) -> None:
    bal = _sub(stmt, "Bal")
    _sub(_sub(_sub(bal, "Tp"), "CdOrPrtry"), "Cd", code)
    _sub(bal, "Amt", _format_amount(balance.amount), Ccy=balance.currency)
    _sub(bal, "CdtDbtInd", "DBIT" if balance.amount < 0 else "CRDT")
    _sub(_sub(bal, "Dt"), "Dt", balance.date.isoformat())


def _summary(stmt: etree._Element, movements: list[tuple[Decimal, bool]]) -> None:
    credits = [amount for amount, credit in movements if credit]
    debits = [amount for amount, credit in movements if not credit]
    summary = _sub(stmt, "TxsSummry")
    total = _sub(summary, "TtlNtries")
    _sub(total, "NbOfNtries", str(len(movements)))
    net = sum(credits, Decimal(0)) - sum(debits, Decimal(0))
    _sub(total, "TtlNetNtryAmt", _format_amount(net))
    _sub(total, "CdtDbtInd", "DBIT" if net < 0 else "CRDT")
    for tag, amounts in (("TtlCdtNtries", credits), ("TtlDbtNtries", debits)):
        part = _sub(summary, tag)
        _sub(part, "NbOfNtries", str(len(amounts)))
        _sub(part, "Sum", _format_amount(sum(amounts, Decimal(0))))


def raw_entry_movement(entry: etree._Element) -> tuple[Decimal, bool]:
    """Amount and direction (credit?) of a passed-through ``Ntry`` element."""
    amount = _decimal(entry.findtext(_q("Amt")))
    return amount, entry.findtext(_q("CdtDbtInd")) == "CRDT"


def _entry(stmt: etree._Element, entry: Entry) -> None:
    ntry = _sub(stmt, "Ntry")
    if entry.entry_reference:
        _sub(ntry, "NtryRef", _text(entry.entry_reference, 35))
    _sub(ntry, "Amt", _format_amount(entry.amount), Ccy=entry.currency)
    _sub(ntry, "CdtDbtInd", "CRDT" if entry.credit else "DBIT")
    if entry.reversal:
        _sub(ntry, "RvslInd", "true")
    _sub(ntry, "Sts", "BOOK")
    _sub(_sub(ntry, "BookgDt"), "Dt", entry.booking_date.isoformat())
    if entry.value_date:
        _sub(_sub(ntry, "ValDt"), "Dt", entry.value_date.isoformat())
    account_servicer_reference = _text(entry.account_servicer_reference, 35)
    if account_servicer_reference:
        _sub(ntry, "AcctSvcrRef", account_servicer_reference)
    _bank_transaction_code(ntry, entry.bank_transaction_code)
    if entry.batch or entry.transactions:
        details = _sub(ntry, "NtryDtls")
        if entry.batch:
            batch = _sub(details, "Btch")
            if entry.batch_count is not None:
                _sub(batch, "NbOfTxs", str(entry.batch_count))
        for tx in entry.transactions:
            _transaction(details, tx)
    info = _text(entry.additional_info, 500)
    if info:
        _sub(ntry, "AddtlNtryInf", info)


def _bank_transaction_code(ntry: etree._Element, code: BankTransactionCode) -> None:
    btc = _sub(ntry, "BkTxCd")
    if code.domain and code.family and code.sub_family:
        domain = _sub(btc, "Domn")
        _sub(domain, "Cd", code.domain)
        family = _sub(domain, "Fmly")
        _sub(family, "Cd", code.family)
        _sub(family, "SubFmlyCd", code.sub_family)
    proprietary = _text(code.proprietary, 35)
    if proprietary or not (code.domain and code.family and code.sub_family):
        prtry = _sub(btc, "Prtry")
        _sub(prtry, "Cd", proprietary or "NOTPROVIDED")
        issuer = _text(code.proprietary_issuer, 35)
        if issuer:
            _sub(prtry, "Issr", issuer)


def _transaction(details: etree._Element, tx: TransactionDetails) -> None:
    txdtls = _sub(details, "TxDtls")
    references = [
        ("AcctSvcrRef", _text(tx.account_servicer_reference, 35)),
        ("EndToEndId", _text(tx.end_to_end_id, 35)),
        ("MndtId", _text(tx.mandate_id, 35)),
    ]
    if any(value for _, value in references):
        refs = _sub(txdtls, "Refs")
        for tag, value in references:
            if value:
                _sub(refs, tag, value)

    debtor, creditor = tx.debtor, tx.creditor
    creditor_id = _text(tx.creditor_id, 35)
    ultimate_debtor = _text(tx.ultimate_debtor, 70)
    ultimate_creditor = _text(tx.ultimate_creditor, 70)
    has_debtor = debtor is not None and (debtor.has_party_data() or debtor.has_account_data())
    has_creditor = creditor_id is not None or (
        creditor is not None and (creditor.has_party_data() or creditor.has_account_data())
    )
    if has_debtor or has_creditor or ultimate_debtor or ultimate_creditor:
        parties = _sub(txdtls, "RltdPties")
        if debtor is not None and debtor.has_party_data():
            _party(parties, "Dbtr", debtor.name, None)
        if debtor is not None and debtor.has_account_data():
            _account(parties, "DbtrAcct", debtor)
        if ultimate_debtor:
            _sub(_sub(parties, "UltmtDbtr"), "Nm", ultimate_debtor)
        if (creditor is not None and creditor.has_party_data()) or creditor_id:
            _party(parties, "Cdtr", creditor.name if creditor else None, creditor_id)
        if creditor is not None and creditor.has_account_data():
            _account(parties, "CdtrAcct", creditor)
        if ultimate_creditor:
            _sub(_sub(parties, "UltmtCdtr"), "Nm", ultimate_creditor)

    agents = [(tag, party) for tag, party in (("DbtrAgt", debtor), ("CdtrAgt", creditor))
              if party is not None and party.has_agent_data()]
    if agents:
        related_agents = _sub(txdtls, "RltdAgts")
        for tag, party in agents:
            _agent(related_agents, tag, party)

    remittance = [chunk for line in tx.remittance for chunk in _chunks(_text(line, 4 * 140) or "", 140)]
    if remittance:
        rmtinf = _sub(txdtls, "RmtInf")
        for line in remittance:
            _sub(rmtinf, "Ustrd", line)


def _party(parent: etree._Element, tag: str, name: str | None, creditor_id: str | None) -> None:
    party = _sub(parent, tag)
    clean_name = _text(name, 70)
    if clean_name:
        _sub(party, "Nm", clean_name)
    if creditor_id:
        other = _sub(_sub(_sub(party, "Id"), "PrvtId"), "Othr")
        _sub(other, "Id", creditor_id)
        _sub(_sub(other, "SchmeNm"), "Prtry", "SEPA")


def _account(parent: etree._Element, tag: str, party: Party) -> None:
    account_id = _sub(_sub(parent, tag), "Id")
    if party.iban and _IBAN.match(party.iban):
        _sub(account_id, "IBAN", party.iban)
    else:
        _sub(_sub(account_id, "Othr"), "Id", _text(party.other_account or party.iban, 34))


def _agent(parent: etree._Element, tag: str, party: Party) -> None:
    institution = _sub(_sub(parent, tag), "FinInstnId")
    if party.bic and _BIC.match(party.bic):
        _sub(institution, "BIC", party.bic)
    elif party.bank_code:
        member = _sub(institution, "ClrSysMmbId")
        _sub(_sub(member, "ClrSysId"), "Cd", "DEBLZ")
        _sub(member, "MmbId", party.bank_code)
    else:
        _sub(_sub(institution, "Othr"), "Id", _text(party.bic, 35))


# --- validation ----------------------------------------------------------------------------


@functools.cache
def _schema() -> etree.XMLSchema:
    source = importlib.resources.files("fints2camt").joinpath("schemas", "camt.053.001.02.xsd")
    with source.open("rb") as handle:
        return etree.XMLSchema(etree.parse(handle, _safe_parser()))


def validate_camt053(document: bytes) -> None:
    """Raise :class:`CamtValidationError` unless ``document`` is valid camt.053.001.02."""
    try:
        tree = etree.fromstring(document, _safe_parser())
    except etree.XMLSyntaxError as exc:
        # lxml's message may quote the document; the line is enough to find the spot.
        raise CamtValidationError(f"camt.053 is not well-formed XML (line {exc.lineno})") from None
    schema = _schema()
    if not schema.validate(tree):
        errors = "; ".join(_schema_error(e) for e in list(schema.error_log)[:5])
        raise CamtValidationError(f"camt.053 does not validate against the XSD: {errors}")


_ERROR_ELEMENT = re.compile(r"^Element '(?:\{[^}]*\})?([^']+)'")
_ERROR_FACET = re.compile(r"\[facet '([A-Za-z]+)'\]")


def _schema_error(error: etree._LogEntry) -> str:
    """Line, element and facet of an XSD error — never the value: lxml's message quotes the
    offending content (names, remittance information) of the statement."""
    parts = [f"line {error.line}"]
    element = _ERROR_ELEMENT.match(error.message or "")
    if element:
        parts.append(f"element {element.group(1)}")
    facet = _ERROR_FACET.search(error.message or "")
    if facet:
        parts.append(f"facet {facet.group(1)}")
    parts.append(str(error.type_name))
    return ", ".join(parts)


def _safe_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, remove_blank_text=True, huge_tree=False)


# --- reading the bank's camt ---------------------------------------------------------------


@dataclass
class BankCamt:
    """The booked content of the bank's camt documents for one account."""

    entries: list[Entry] = field(default_factory=list)
    raw_entries: list[etree._Element] = field(default_factory=list)
    opening: Balance | None = None
    closing: Balance | None = None
    currency: str | None = None


def read_bank_camt(documents: Iterable[bytes], iban: str) -> BankCamt:
    """Collect the booked entries and the balances of ``iban`` from camt.052/053/054 documents."""
    parsed: list[tuple[str, etree._Element]] = []
    for document in documents:
        try:
            root = etree.fromstring(document, _safe_parser())
        except etree.XMLSyntaxError as exc:
            raise CamtError(f"the bank's camt is not well-formed XML: {exc}") from exc
        namespace = etree.QName(root).namespace or ""
        match = _CAMT_NS.match(namespace)
        if not match:
            raise CamtError(f"unsupported camt message from the bank: {namespace or 'no namespace'}")
        parsed.append((match.group(1), root))

    pass_through = all(version == "02" for version, _ in parsed)
    result = BankCamt()
    for _, root in parsed:
        for container in _containers(root, iban):
            _collect_balances(result, container)
            for ntry in container:
                if _local(ntry) != "Ntry" or _status(ntry) != "BOOK":
                    continue
                if pass_through:
                    result.raw_entries.append(_to_camt053_namespace(ntry))
                else:
                    result.entries.append(_entry_from_xml(ntry))
            currency = _child_text(container, "Acct", "Ccy")
            if currency and not result.currency:
                result.currency = currency
    _assign_raw_references(iban, result.raw_entries)
    return result


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname if isinstance(element.tag, str) else ""


def _children(element: etree._Element, name: str) -> list[etree._Element]:
    return [child for child in element if _local(child) == name]


def _child(element: etree._Element | None, *path: str) -> etree._Element | None:
    for name in path:
        if element is None:
            return None
        found = _children(element, name)
        element = found[0] if found else None
    return element


def _child_text(element: etree._Element | None, *path: str) -> str | None:
    found = _child(element, *path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _containers(root: etree._Element, iban: str) -> list[etree._Element]:
    containers = [
        element for element in root.iter()
        if isinstance(element.tag, str) and _local(element) in ("Stmt", "Rpt", "Ntfctn")
    ]
    selected = []
    for container in containers:
        container_iban = _child_text(container, "Acct", "Id", "IBAN")
        if container_iban is None or container_iban.replace(" ", "").upper() == iban:
            selected.append(container)
    return selected


def _status(ntry: etree._Element) -> str | None:
    status = _child(ntry, "Sts")
    if status is None:
        return None
    code = _child_text(status, "Cd")  # since version 08 the status is a choice element
    return code or (status.text or "").strip() or None


def _decimal(text: str | None) -> Decimal:
    try:
        return Decimal((text or "").strip())
    except InvalidOperation as exc:
        raise CamtError(f"invalid amount {text!r} in the bank's camt") from exc


def _date(element: etree._Element | None) -> datetime.date | None:
    if element is None:
        return None
    text = _child_text(element, "Dt") or _child_text(element, "DtTm")
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise CamtError(f"invalid date {text!r} in the bank's camt") from exc


def _collect_balances(result: BankCamt, container: etree._Element) -> None:
    for bal in _children(container, "Bal"):
        code = _child_text(bal, "Tp", "CdOrPrtry", "Cd")
        amount_element = _child(bal, "Amt")
        if code is None or amount_element is None:
            continue
        amount = _decimal(amount_element.text)
        if _child_text(bal, "CdtDbtInd") == "DBIT":
            amount = -amount
        balance = Balance(
            amount=amount,
            currency=amount_element.get("Ccy", ""),
            date=_date(_child(bal, "Dt")) or datetime.date.min,
        )
        if code in _OPENING_CODES and result.opening is None:
            result.opening = balance
        elif code in _CLOSING_CODES:
            result.closing = balance


def _to_camt053_namespace(element: etree._Element) -> etree._Element:
    moved = copy.deepcopy(element)
    for node in moved.iter():
        if isinstance(node.tag, str):
            node.tag = _q(etree.QName(node).localname)
    etree.cleanup_namespaces(moved)
    return moved


def _assign_raw_references(iban: str, entries: list[etree._Element]) -> None:
    """Give passed-through entries without ``NtryRef`` a deterministic one."""
    seen: dict[bytes, int] = {}
    for ntry in entries:
        if _child(ntry, "NtryRef") is not None:
            continue
        canonical = etree.tostring(ntry, method="c14n")
        occurrence = seen.get(canonical, 0)
        seen[canonical] = occurrence + 1
        reference = etree.Element(_q("NtryRef"))
        reference.text = deterministic_reference(f"{iban}\x1e{canonical.decode()}\x1e{occurrence}")
        ntry.insert(0, reference)


def _entry_from_xml(ntry: etree._Element) -> Entry:
    amount_element = _child(ntry, "Amt")
    booking_date = _date(_child(ntry, "BookgDt")) or _date(_child(ntry, "ValDt"))
    if amount_element is None or booking_date is None:
        raise CamtError("a booked entry in the bank's camt lacks its amount or booking date")
    code = BankTransactionCode(
        domain=_child_text(ntry, "BkTxCd", "Domn", "Cd"),
        family=_child_text(ntry, "BkTxCd", "Domn", "Fmly", "Cd"),
        sub_family=_child_text(ntry, "BkTxCd", "Domn", "Fmly", "SubFmlyCd"),
        proprietary=_child_text(ntry, "BkTxCd", "Prtry", "Cd"),
        proprietary_issuer=_child_text(ntry, "BkTxCd", "Prtry", "Issr"),
    )
    details = _children(ntry, "NtryDtls")
    transactions = [
        _transaction_from_xml(tx) for detail in details for tx in _children(detail, "TxDtls")
    ]
    batch_size = _child_text(_child(details[0], "Btch") if details else None, "NbOfTxs")
    batch = bool(details and _child(details[0], "Btch") is not None) or len(transactions) > 1
    return Entry(
        amount=_decimal(amount_element.text),
        currency=amount_element.get("Ccy", ""),
        credit=_child_text(ntry, "CdtDbtInd") == "CRDT",
        booking_date=booking_date,
        value_date=_date(_child(ntry, "ValDt")),
        reversal=_child_text(ntry, "RvslInd") == "true",
        account_servicer_reference=_child_text(ntry, "AcctSvcrRef"),
        entry_reference=_child_text(ntry, "NtryRef"),
        bank_transaction_code=code,
        batch=batch,
        batch_count=int(batch_size) if batch_size and batch_size.isdigit() else (
            len(transactions) if len(transactions) > 1 else None
        ),
        transactions=transactions,
        additional_info=_child_text(ntry, "AddtlNtryInf"),
    )


def _transaction_from_xml(tx: etree._Element) -> TransactionDetails:
    parties = _child(tx, "RltdPties")
    agents = _child(tx, "RltdAgts")
    creditor_party = _party_element(parties, "Cdtr")
    remittance = _child(tx, "RmtInf")
    return TransactionDetails(
        end_to_end_id=_child_text(tx, "Refs", "EndToEndId"),
        mandate_id=_child_text(tx, "Refs", "MndtId"),
        account_servicer_reference=_child_text(tx, "Refs", "AcctSvcrRef"),
        debtor=_party_from_xml(parties, agents, "Dbtr"),
        creditor=_party_from_xml(parties, agents, "Cdtr"),
        creditor_id=_child_text(creditor_party, "Id", "PrvtId", "Othr", "Id"),
        ultimate_debtor=_child_text(_party_element(parties, "UltmtDbtr"), "Nm"),
        ultimate_creditor=_child_text(_party_element(parties, "UltmtCdtr"), "Nm"),
        remittance=[
            (element.text or "").strip()
            for element in (_children(remittance, "Ustrd") if remittance is not None else [])
            if (element.text or "").strip()
        ],
    )


def _party_element(parties: etree._Element | None, role: str) -> etree._Element | None:
    """The party identification of ``role`` (since version 08 nested in ``Pty``)."""
    element = _child(parties, role)
    nested = _child(element, "Pty")
    return nested if nested is not None else element


def _party_from_xml(parties: etree._Element | None, agents: etree._Element | None, role: str) -> Party | None:
    party = Party(
        name=_child_text(_party_element(parties, role), "Nm"),
        iban=_child_text(parties, f"{role}Acct", "Id", "IBAN"),
        other_account=_child_text(parties, f"{role}Acct", "Id", "Othr", "Id"),
        bic=_child_text(agents, f"{role}Agt", "FinInstnId", "BIC")
        or _child_text(agents, f"{role}Agt", "FinInstnId", "BICFI"),
        bank_code=_child_text(agents, f"{role}Agt", "FinInstnId", "ClrSysMmbId", "MmbId"),
    )
    if party.has_party_data() or party.has_account_data() or party.has_agent_data():
        return party
    return None
