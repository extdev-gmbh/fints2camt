"""Shared helpers. All bank data in the fixtures is synthetic (made-up names, IBANs, ids)."""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

FIXTURES = Path(__file__).parent / "fixtures"
OWN_IBAN = "DE89370400440532013000"
NS = {"c": "urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def parse_xml(document: bytes) -> etree._Element:
    return etree.fromstring(document)


@pytest.fixture
def mt940_text() -> str:
    return fixture_text("mt940_synthetic.sta")
