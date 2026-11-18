# ofxstatement-consorsbank - Consorsbank PDF statement plugin for ofxstatement
# Copyright (C) 2026  Eduard Ralph
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
Consorsbank depot (securities) documents → OFX investment statements.

Each Consorsbank depot document describes exactly one event, so each parsed
file yields a ``Statement`` with at most one ``InvestStatementLine``:

  ORDERABRECHNUNG KAUF/VERKAUF   → BUYSTOCK/SELLSTOCK (BUYDEBT/SELLDEBT for bonds)
  Ertragsgutschrift              → INCOME / DIV
  Zinsgutschrift                 → INCOME / INTEREST
  Vorabpauschale                 → INVEXPENSE when tax was debited, else nothing

pdfplumber drops most spaces in these documents ("PreisproAnteil"), so all
field matching runs on a whitespace-free copy of the text.  Only the security
name / WKN / ISIN row is read line-based, because the name needs its spaces.

Transaction IDs are chosen so that re-issued documents collide with the
original on purpose:

  orders   – Consorsbank order number (a "KAUF - KORREKTUR" reuses it)
  income   – hash of ISIN, Schlusstag and Bestand (a "Neuabrechnung" reuses
             them; two payouts for the same ISIN held in two custody
             locations differ in Bestand and stay distinct)
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from decimal import Decimal

from ofxstatement.exceptions import ParseError
from ofxstatement.statement import InvestStatementLine, Statement

logger = logging.getLogger(__name__)

CONSORSBANK_BIC = "CSDBDE71XXX"

# pdfplumber leaves some glyphs of the older PDFs as (cid:NNN) placeholders.
_CID_MAP = {
    "(cid:252)": "ü",
    "(cid:228)": "ä",
    "(cid:246)": "ö",
    "(cid:223)": "ß",
    "(cid:220)": "Ü",
    "(cid:196)": "Ä",
    "(cid:214)": "Ö",
}

_AMT = r"(\d{1,3}(?:\.\d{3})*,\d{2})"  # German money amount, 2 decimals
_NUM = r"(\d{1,3}(?:\.\d{3})*,\d+)"  # German number, any decimals
_DATE = r"(\d{2}\.\d{2}\.\d{4})"

ORDER_HDR_RE = re.compile(
    r"ORDERABRECHNUNG(KAUF|VERKAUF)AM"
    + _DATE
    + r"UM(\d{2}:\d{2}:\d{2}).*?NR\.(\d+\.\d+)"
)
UNITS_RE = re.compile(r"EinheitUmsatz(?:Fälligkeit)?(ST|EUR)" + _NUM)
PRICE_RE = re.compile(r"(?:Kurs|PreisproAnteil)" + _NUM + r"(EUR|%)")
KURSWERT_RE = re.compile(r"Kurswert(?:EUR)?" + _AMT)
# Newer layout: "zulasten Konto-Nr. 0700880049   1.240,44 EUR"
TOTAL_NEW_RE = re.compile(r"zu(lasten|gunsten)Konto-Nr\.\d{10}" + _AMT + r"EUR")
# 2019/2020 layout: "Wert 03.10.2019   EUR 150,00" then "zulasten Konto-Nr. …"
TOTAL_OLD_RE = re.compile(
    r"Wert\d{2}\.\d{2}\.\d{4}EUR" + _AMT + r"zu(lasten|gunsten)Konto-Nr\."
)
STUECKZINS_RE = re.compile(r"Stückzins.*?Tage" + _AMT + r"EUR")

INCOME_HDR_RE = re.compile(r"(Ertragsgutschrift|Zinsgutschrift)")
VORAB_HDR_RE = re.compile(r"VorabpauschaleWertpapierbezeichnung")
BRUTTO_RE = re.compile(r"Brutto" + _AMT + r"EUR")
NETTO_RE = re.compile(r"Netto(zugunsten|zulasten)IBAN(?:DE\d{20})?" + _AMT + r"EUR")
VALUTA_RE = re.compile(r"Valuta" + _DATE)
SCHLUSSTAG_RE = re.compile(r"Schlusstag" + _DATE)
DOC_DATE_RE = re.compile(r"Datum:" + _DATE)
BESTAND_RE = re.compile(r"Bestand(?:Kupontag)?" + _NUM + r"(Stück|EUR)")
DEPOT_NR_RE = re.compile(r"Depotnummer:?(\d{10})")

SECURITY_ROW_RE = re.compile(r"^(.*?)\s+(\S{6})\s+([A-Z]{2}[A-Z0-9]{9}\d)\s*$")
SECURITY_HDR_RE = re.compile(r"bezeichnung\s+WKN\s+ISIN", re.IGNORECASE)


def _clean(text: str) -> str:
    for cid, char in _CID_MAP.items():
        text = text.replace(cid, char)
    return text


def _dec(raw: str) -> Decimal:
    return Decimal(raw.replace(".", "").replace(",", "."))


def _date(raw: str) -> datetime:
    return datetime.strptime(raw, "%d.%m.%Y")


def _hash_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def detect_depot_document(lines: list[str]) -> str | None:
    """Return 'order', 'income' or 'vorab' for supported depot documents."""
    compact = re.sub(r"\s+", "", _clean("\n".join(lines[:80])))
    if "ORDERABRECHNUNG" in compact:
        return "order"
    if VORAB_HDR_RE.search(compact):
        return "vorab"
    if INCOME_HDR_RE.search(compact) and "Wertpapierbezeichnung" in compact:
        return "income"
    return None


class DepotDocument:
    """Parse one Consorsbank depot document into an investment Statement."""

    def __init__(self, lines: list[str], filename: str = "") -> None:
        self.lines = [_clean(line) for line in lines]
        self.compact = re.sub(r"\s+", "", "\n".join(self.lines))
        self.filename = filename

    # ── helpers ────────────────────────────────────────────────────────────

    def _require(self, regex: re.Pattern[str], what: str) -> re.Match[str]:
        m = regex.search(self.compact)
        if not m:
            raise ParseError(0, f"{self.filename}: could not find {what}")
        return m

    def _security(self) -> tuple:
        """(name, wkn, isin) from the row below the 'Bezeichnung WKN ISIN' header."""
        for i, line in enumerate(self.lines):
            if SECURITY_HDR_RE.search(line):
                for nxt in self.lines[i + 1 : i + 3]:
                    m = SECURITY_ROW_RE.match(nxt.strip())
                    if m:
                        return m.group(1).strip(), m.group(2), m.group(3)
        raise ParseError(0, f"{self.filename}: security name/WKN/ISIN row not found")

    def _statement(self, line: InvestStatementLine | None) -> Statement:
        m = DEPOT_NR_RE.search(self.compact)
        stmt = Statement(
            account_id=m.group(1) if m else "UNKNOWN",
            bank_id=CONSORSBANK_BIC,
            currency="EUR",
            account_type="INVESTMENT",
        )
        stmt.broker_id = CONSORSBANK_BIC
        if line is not None:
            # ofxstatement 0.9.x's assert_valid() rejects BUYDEBT/SELLDEBT
            # (if/elif chain bug), so only validate the other types.
            if not (line.trntype or "").endswith("DEBT"):
                line.assert_valid()
            stmt.invest_lines.append(line)
            stmt.start_date = stmt.end_date = line.date
        return stmt

    # ── document kinds ─────────────────────────────────────────────────────

    def parse(self, kind: str) -> Statement:
        if kind == "order":
            return self._statement(self._order())
        if kind == "income":
            return self._statement(self._income())
        if kind == "vorab":
            return self._statement(self._vorab())
        raise ValueError(kind)

    def _order(self) -> InvestStatementLine:
        hdr = self._require(ORDER_HDR_RE, "order header (KAUF/VERKAUF AM …)")
        side, day, clock, order_nr = hdr.groups()
        name, wkn, isin = self._security()
        unit_kind, units_raw = self._require(
            UNITS_RE, "units (Einheit/Umsatz)"
        ).groups()
        price_raw, price_unit = self._require(PRICE_RE, "price").groups()
        kurswert = _dec(self._require(KURSWERT_RE, "Kurswert").group(1))

        m_new = TOTAL_NEW_RE.search(self.compact)
        m_old = TOTAL_OLD_RE.search(self.compact)
        if m_new:
            total = _dec(m_new.group(2))
        elif m_old:
            total = _dec(m_old.group(1))
        else:
            raise ParseError(
                0, f"{self.filename}: booked total (zulasten/zugunsten) not found"
            )

        accrued = Decimal(0)
        m_sz = STUECKZINS_RE.search(self.compact)
        if m_sz:
            accrued = _dec(m_sz.group(1))

        is_buy = side == "KAUF"
        is_bond = unit_kind == "EUR"
        if is_buy:
            fees = total - kurswert - accrued
        else:
            fees = kurswert + accrued - total
        if total == 0:
            # Seen on Sparplan executions later re-issued as "KAUF - KORREKTUR"
            # with the same order number and the real debit.  Fall back to the
            # Kurswert so the line stays valid; the correction (same FITID)
            # supersedes it downstream.
            logger.warning(
                "%s: order %s books 0,00 EUR — using Kurswert %s; expect a "
                "'KAUF - KORREKTUR' with the same order number",
                self.filename,
                order_nr,
                kurswert,
            )
            total = kurswert + accrued if is_buy else kurswert - accrued
            fees = Decimal(0)
        if fees < 0:
            logger.warning(
                "%s: order %s has negative fees %s (Kurswert %s, total %s)",
                self.filename,
                order_nr,
                fees,
                kurswert,
                total,
            )

        memo = f"{side} {name} ({wkn}) Order {order_nr}"
        if accrued:
            memo += f", Stückzins {accrued} EUR"
        if price_unit == "%":
            memo += f", Kurs {_dec(price_raw)} %"

        if is_bond:
            trntype, detailed = ("BUYDEBT" if is_buy else "SELLDEBT"), None
        else:
            trntype = "BUYSTOCK" if is_buy else "SELLSTOCK"
            detailed = "BUY" if is_buy else "SELL"

        units = _dec(units_raw)
        line = InvestStatementLine(
            id=f"ord{order_nr}",
            date=datetime.strptime(f"{day} {clock}", "%d.%m.%Y %H:%M:%S"),
            memo=memo,
            trntype=trntype,
            trntype_detailed=detailed,
            security_id=isin,
            amount=-total if is_buy else total,
        )
        line.units = units if is_buy else -units
        # Bonds are quoted in % of nominal; express the price per 1 EUR nominal
        # so units × unit_price == Kurswert like for shares.
        price = _dec(price_raw)
        line.unit_price = price / 100 if price_unit == "%" else price
        line.fees = fees if fees > 0 else None
        logger.info(
            "Order %s: %s %s %s @ %s, total %s",
            order_nr,
            side,
            units,
            isin,
            price,
            total,
        )
        return line

    def _income(self) -> InvestStatementLine:
        kind = self._require(INCOME_HDR_RE, "Ertrags-/Zinsgutschrift").group(1)
        name, wkn, isin = self._security()
        brutto = _dec(self._require(BRUTTO_RE, "Brutto").group(1))
        direction, netto_raw = self._require(NETTO_RE, "Netto").groups()
        netto = _dec(netto_raw)
        if direction == "zulasten":
            netto = -netto
        valuta = VALUTA_RE.search(self.compact)
        doc_date = self._require(DOC_DATE_RE, "Datum")
        date = _date(valuta.group(1) if valuta else doc_date.group(1))
        schluss = SCHLUSSTAG_RE.search(self.compact)
        bestand = BESTAND_RE.search(self.compact)
        withheld = brutto - netto

        ref_date = schluss.group(1) if schluss else date.strftime("%d.%m.%Y")
        line = InvestStatementLine(
            id=_hash_id(isin, ref_date, bestand.group(1) if bestand else ""),
            date=date,
            memo=f"{kind} {name} ({wkn})"
            + (f", Bestand {bestand.group(1)} {bestand.group(2)}" if bestand else ""),
            trntype="INCOME",
            trntype_detailed="INTEREST" if kind == "Zinsgutschrift" else "DIV",
            security_id=isin,
            amount=netto,
        )
        line.fees = withheld if withheld > 0 else None
        logger.info("%s %s: brutto %s, netto %s", kind, isin, brutto, netto)
        return line

    def _vorab(self) -> InvestStatementLine | None:
        name, wkn, isin = self._security()
        # 2020-era letters have no Netto line at all (nothing was booked).
        m_netto = NETTO_RE.search(self.compact)
        direction, netto = (
            (m_netto.group(1), _dec(m_netto.group(2))) if m_netto else ("", Decimal(0))
        )
        doc_date = _date(self._require(DOC_DATE_RE, "Datum").group(1))
        if netto == 0:
            logger.info(
                "Vorabpauschale %s: no tax debited (covered by Freistellungsauftrag) "
                "— 0 transactions",
                isin,
            )
            return None
        valuta = VALUTA_RE.search(self.compact)
        schluss = SCHLUSSTAG_RE.search(self.compact)
        line = InvestStatementLine(
            id=_hash_id(
                "VORAB", isin, schluss.group(1) if schluss else doc_date.isoformat()
            ),
            date=_date(valuta.group(1)) if valuta else doc_date,
            memo=f"Steuer Vorabpauschale {name} ({wkn})",
            trntype="INVEXPENSE",
            security_id=isin,
            amount=-netto if direction == "zulasten" else netto,
        )
        return line
