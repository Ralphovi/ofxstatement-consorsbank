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

  ORDERABRECHNUNG KAUF/VERKAUF   → BUYSTOCK/SELLSTOCK (bonds too: ofxstatement's
                                   writer declares every security as STOCKINFO
                                   and cannot write SELLDEBT's SELLREASON)
  Ertragsgutschrift              → INCOME / DIV
  Zinsgutschrift                 → INCOME / INTEREST
  Vorabpauschale                 → INVEXPENSE when tax was debited, else nothing

pdfplumber drops most spaces in these documents ("PreisproAnteil"), so all
field matching runs on a whitespace-free copy of the text.  Only the security
name / WKN / ISIN row is read line-based, because the name needs its spaces.

Transaction IDs:

  orders   – Consorsbank order number
  income   – hash of ISIN, Schlusstag and Bestand (two payouts for the same
             ISIN held in two custody locations differ in Bestand)
  Vorabp.  – hash of ISIN, Schlusstag and Bestand

A re-downloaded document therefore yields the same ID and is skipped by
importers that deduplicate on it.  The re-issues seen so far are Sparplan
executions first booked with 0,00 EUR and later sent as "KAUF - KORREKTUR";
the 0,00 document yields no transaction, so the correction is the only one.

Fail-closed rules: the booked total must equal Kurswert ± Stückzins ± the
listed fee lines (Grundgebühr, Provision, Handelsplatzkosten), the booking
direction must match KAUF/VERKAUF, the depot number and Bestand must be
present, and documents with non-EUR amounts are rejected.
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
# German quantity whose decimals are optional: holdings print "143 Stück" or
# "1.000 EUR" when whole.
_QTY = r"(\d{1,3}(?:\.\d{3})*(?:,\d+)?)"
_DATE = r"(\d{2}\.\d{2}\.\d{4})"

# Real corrections (2024) repeat the plain "KAUF AM" header and say KORREKTUR
# only in the file name; an optional "- KORREKTUR" marker is accepted as well.
# Bounded gaps (.{0,N}?) keep searches linear on crafted text: the compact
# text is one long line.
ORDER_HDR_RE = re.compile(
    r"ORDERABRECHNUNG(KAUF|VERKAUF)(?:-?KORREKTUR)?AM"
    + _DATE
    + r"UM(\d{2}:\d{2}:\d{2}).{0,80}?NR\.(\d+\.\d+)"
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
STUECKZINS_RE = re.compile(r"Stückzins.{0,80}?Tage" + _AMT + r"EUR")
# Fee lines between Kurswert and the booked total; "EUR" before (2019/2020
# layout) or after the amount.
FEE_RE = re.compile(
    r"(?:abzgl\.)?(Grundgebühr|Provision|Handelsplatzkosten)(?:EUR)?"
    + _AMT
    + r"(?:EUR)?"
)
# An amount directly followed by a currency code other than EUR.
# An amount with a currency code other than EUR directly after ("10,00USD")
# or before ("USD10,00") it.
FOREIGN_CCY_RE = re.compile(
    r"\d,\d+(?!EUR)([A-Z]{3})|(?<![A-Z])(?!EUR)([A-Z]{3})\d{1,3}(?:\.\d{3})*,\d"
)

INCOME_HDR_RE = re.compile(r"(Ertragsgutschrift|Zinsgutschrift)")
# Re-issued payouts are headed "Ertragsgutschrift / Neuabrechnung".
INCOME_DETECT_RE = re.compile(
    r"(?:Ertragsgutschrift|Zinsgutschrift)(?:/Neuabrechnung)?Wertpapierbezeichnung"
)
# Words that only depot documents contain (none of 667 real account
# statements has them).  A document with these but no recognised header is
# rejected rather than handed to the bank-statement parser.
DEPOT_MARKER_RE = re.compile(r"ORDERABRECHNUNG|Wertpapierbezeichnung")
# Periodic depot statements stay with the bank parser, which warns and
# returns no transactions for them.
PERIODIC_DEPOT_RE = re.compile(r"QUARTALSDEPOTAUSZUG|JAHRESDEPOTAUSZUG")
ORDER_DETECT_RE = re.compile(r"ORDERABRECHNUNG(?:KAUF|VERKAUF)(?:-?KORREKTUR)?AM\d")
VORAB_HDR_RE = re.compile(r"VorabpauschaleWertpapierbezeichnung")
KAPITALERTRAG_RE = re.compile(r"Kapitalertrag" + _AMT + r"EUR")
BRUTTO_RE = re.compile(r"Brutto" + _AMT + r"EUR")
NETTO_RE = re.compile(r"Netto(zugunsten|zulasten)IBAN(?:DE\d{20})?" + _AMT + r"EUR")
VALUTA_RE = re.compile(r"Valuta" + _DATE)
SCHLUSSTAG_RE = re.compile(r"Schlusstag" + _DATE)
DOC_DATE_RE = re.compile(r"Datum:" + _DATE)
BESTAND_RE = re.compile(r"Bestand(?:Kupontag)?" + _QTY + r"(Stück|EUR)")
# Precision of units and prices in ofxstatement's OFX writer.
_OFX_QUANTUM = Decimal("0.00001")
# pdfplumber lines longer than this are boilerplate, never the security row.
_MAX_SECURITY_ROW = 200
DEPOT_NR_RE = re.compile(r"Depotnummer:?(\d{10})")

SECURITY_ROW_RE = re.compile(r"^(.*?)\s+(\S{6})\s+([A-Z]{2}[A-Z0-9]{9}\d)\s*$")
SECURITY_HDR_RE = re.compile(r"bezeichnung\s+WKN\s+ISIN", re.IGNORECASE)


def _clean(text: str) -> str:
    for cid, char in _CID_MAP.items():
        text = text.replace(cid, char)
    return text


def _dec(raw: str) -> Decimal:
    return Decimal(raw.replace(".", "").replace(",", "."))


def _date(raw: str, fmt: str = "%d.%m.%Y") -> datetime:
    try:
        return datetime.strptime(raw, fmt)
    except ValueError as exc:
        raise ParseError(0, f"invalid date {raw!r}") from exc


def _qty_key(raw: str) -> str:
    """Canonical form of a German quantity, so "143" == "143,00000".

    Works on the digits (no Decimal context), so long values are not rounded.
    """
    whole, _, frac = raw.replace(".", "").partition(",")
    whole = whole.lstrip("0") or "0"
    frac = frac.rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def _hash_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def detect_depot_document(lines: list[str]) -> str | None:
    """Return 'order', 'income' or 'vorab' for supported depot documents."""
    # The whole document is scanned, but only for structural headers: a bank
    # statement may mention these words in a booking text.
    compact = re.sub(r"\s+", "", _clean("\n".join(lines)))
    if ORDER_DETECT_RE.search(compact):
        return "order"
    if VORAB_HDR_RE.search(compact):
        return "vorab"
    if INCOME_DETECT_RE.search(compact):
        return "income"
    if DEPOT_MARKER_RE.search(compact) and not PERIODIC_DEPOT_RE.search(compact):
        raise ParseError(
            0, "looks like a depot document, but its layout is not recognised"
        )
    return None


class DepotDocument:
    """Parse one Consorsbank depot document into an investment Statement."""

    def __init__(self, lines: list[str], filename: str = "") -> None:
        self.lines = [_clean(line) for line in lines]
        self.compact = re.sub(r"\s+", "", "\n".join(self.lines))
        self.filename = filename

    # ── helpers ────────────────────────────────────────────────────────────

    def _require_unique(self, regex: re.Pattern[str], what: str) -> re.Match[str]:
        """Like _require, but a second match (two payouts in one file) fails."""
        matches = list(regex.finditer(self.compact))
        if len(matches) > 1:
            raise ParseError(0, f"{self.filename}: {len(matches)} × {what}")
        if not matches:
            raise ParseError(0, f"{self.filename}: could not find {what}")
        return matches[0]

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
                    if len(nxt) > _MAX_SECURITY_ROW:
                        continue
                    m = SECURITY_ROW_RE.match(nxt.strip())
                    if m:
                        return m.group(1).strip(), m.group(2), m.group(3)
        raise ParseError(0, f"{self.filename}: security name/WKN/ISIN row not found")

    def _bestand(self) -> tuple[str, str]:
        """(canonical quantity, unit) of the holding the document refers to."""
        qty, unit = self._require(BESTAND_RE, "Bestand").groups()
        return _qty_key(qty), unit

    def _statement(self, line: InvestStatementLine | None) -> Statement:
        m = self._require(DEPOT_NR_RE, "Depotnummer")
        stmt = Statement(
            account_id=m.group(1),
            bank_id=CONSORSBANK_BIC,
            currency="EUR",
            account_type="INVESTMENT",
        )
        stmt.broker_id = CONSORSBANK_BIC
        if line is not None:
            try:
                line.assert_valid()
            except AssertionError as exc:
                raise ParseError(
                    0, f"{self.filename}: invalid {line.trntype} line: {exc}"
                ) from exc
            stmt.invest_lines.append(line)
            stmt.start_date = stmt.end_date = line.date
        return stmt

    # ── document kinds ─────────────────────────────────────────────────────

    def parse(self, kind: str) -> Statement:
        m = FOREIGN_CCY_RE.search(self.compact)
        if m:
            raise ParseError(
                0,
                f"{self.filename}: amounts in {m.group(1) or m.group(2)} — only "
                "EUR depot "
                "documents are supported",
            )
        if kind == "order":
            return self._statement(self._order())
        if kind == "income":
            return self._statement(self._income())
        if kind == "vorab":
            return self._statement(self._vorab())
        raise ValueError(kind)

    def _order(self) -> InvestStatementLine | None:
        hdr = self._require(ORDER_HDR_RE, "order header (KAUF/VERKAUF AM …)")
        side, day, clock, order_nr = hdr.groups()
        name, wkn, isin = self._security()
        unit_kind, units_raw = self._require(
            UNITS_RE, "units (Einheit/Umsatz)"
        ).groups()
        price_raw, price_unit = self._require(PRICE_RE, "price").groups()
        kurswert = _dec(self._require(KURSWERT_RE, "Kurswert").group(1))

        m_kw = KURSWERT_RE.search(self.compact)
        assert m_kw is not None  # found by _require above
        totals = list(TOTAL_NEW_RE.finditer(self.compact)) + list(
            TOTAL_OLD_RE.finditer(self.compact)
        )
        if len(totals) > 1:
            raise ParseError(
                0, f"{self.filename}: {len(totals)} booked totals — expected one"
            )
        m_new = TOTAL_NEW_RE.search(self.compact)
        m_old = TOTAL_OLD_RE.search(self.compact)
        if m_new:
            direction, total = m_new.group(1), _dec(m_new.group(2))
            total_at = m_new.start()
        elif m_old:
            total, direction = _dec(m_old.group(1)), m_old.group(2)
            total_at = m_old.start()
        else:
            raise ParseError(
                0, f"{self.filename}: booked total (zulasten/zugunsten) not found"
            )

        is_buy = side == "KAUF"
        if direction != ("lasten" if is_buy else "gunsten"):
            raise ParseError(
                0,
                f"{self.filename}: {side} booked zu{direction} — direction does "
                "not match the order side",
            )

        is_bond = unit_kind == "EUR"
        units = _dec(units_raw)
        # Bonds are quoted in % of nominal; express the price per 1 EUR nominal
        # so units × unit_price == Kurswert like for shares.
        price = _dec(price_raw)
        unit_price = price / 100 if price_unit == "%" else price
        if abs(units * unit_price - kurswert) > Decimal("0.005"):
            raise ParseError(
                0,
                f"{self.filename}: order {order_nr}: units × price "
                f"{units * unit_price} does not match Kurswert {kurswert}",
            )

        # ofxstatement writes units and prices with 5 decimals; refuse values
        # that would be rounded (real documents: 5 and 6 with a trailing 0).
        for what, value in (("units", units), ("price", unit_price)):
            if value != value.quantize(_OFX_QUANTUM) or value <= 0:
                raise ParseError(
                    0,
                    f"{self.filename}: order {order_nr}: {what} cannot be "
                    "written to OFX (5 decimals) without rounding",
                )

        trade_date = _date(f"{day} {clock}", "%d.%m.%Y %H:%M:%S")

        if total == 0:
            # Sparplan executions are sometimes booked with 0,00 EUR and later
            # re-issued as "KAUF - KORREKTUR" with the same order number and
            # the real debit.  Emit nothing, so the correction is the only
            # transaction for this order.
            logger.warning(
                "%s: order %s books 0,00 EUR — no transaction; import the "
                "'KAUF - KORREKTUR' with the same order number",
                self.filename,
                order_nr,
            )
            return None

        accrued = Decimal(0)
        m_sz = STUECKZINS_RE.search(self.compact)
        if m_sz:
            accrued = _dec(m_sz.group(1))

        fee_area = self.compact[m_kw.end() : total_at]
        fees = sum((_dec(m.group(2)) for m in FEE_RE.finditer(fee_area)), Decimal(0))
        expected = kurswert + accrued + fees if is_buy else kurswert + accrued - fees
        if expected != total:
            raise ParseError(
                0,
                f"{self.filename}: order {order_nr} books {total} EUR, but "
                f"Kurswert {kurswert} + Stückzins {accrued} "
                f"{'+' if is_buy else '-'} fees {fees} = {expected} — "
                "unrecognised deduction (e.g. tax)",
            )

        memo = f"{side} {name} ({wkn}) Order {order_nr}"
        if is_bond:
            memo += ", Anleihe (Nominal EUR)"
        if accrued:
            memo += f", Stückzins {accrued} EUR"
        if price_unit == "%":
            memo += f", Kurs {_dec(price_raw)} %"

        trntype = "BUYSTOCK" if is_buy else "SELLSTOCK"
        detailed = "BUY" if is_buy else "SELL"

        line = InvestStatementLine(
            id=f"ord{order_nr}",
            date=trade_date,
            memo=memo,
            trntype=trntype,
            trntype_detailed=detailed,
            security_id=isin,
            amount=-total if is_buy else total,
        )
        line.units = units if is_buy else -units
        line.unit_price = unit_price
        line.fees = fees if fees > 0 else None
        # No amounts, units or prices in logs (see README, "Debug logging").
        logger.info("Order %s: %s %s", order_nr, side, isin)
        return line

    def _income(self) -> InvestStatementLine:
        kind = self._require(INCOME_HDR_RE, "Ertrags-/Zinsgutschrift").group(1)
        name, wkn, isin = self._security()
        brutto = _dec(self._require_unique(BRUTTO_RE, "Brutto").group(1))
        direction, netto_raw = self._require_unique(NETTO_RE, "Netto").groups()
        if direction != "zugunsten":
            raise ParseError(
                0, f"{self.filename}: {kind} with Netto zulasten is not supported"
            )
        netto = _dec(netto_raw)
        if netto > brutto:
            raise ParseError(
                0, f"{self.filename}: {kind} Netto exceeds Brutto — not supported"
            )
        valuta = VALUTA_RE.search(self.compact)
        doc_date = self._require(DOC_DATE_RE, "Datum")
        date = _date(valuta.group(1) if valuta else doc_date.group(1))
        # Schlusstag is part of the FITID; a fallback date would make the
        # ID depend on what one extraction happened to find.
        ref_date = self._require(SCHLUSSTAG_RE, "Schlusstag").group(1)
        qty, unit = self._bestand()
        withheld = brutto - netto

        line = InvestStatementLine(
            id=_hash_id(kind, isin, ref_date, qty),
            date=date,
            memo=f"{kind} {name} ({wkn}), Bestand {qty} {unit}",
            trntype="INCOME",
            trntype_detailed="INTEREST" if kind == "Zinsgutschrift" else "DIV",
            security_id=isin,
            amount=netto,
        )
        line.fees = withheld if withheld > 0 else None
        logger.info("%s %s", kind, isin)
        return line

    def _vorab(self) -> InvestStatementLine | None:
        name, wkn, isin = self._security()
        # 2020-era letters have no Netto line at all; that is accepted only
        # when they state a Kapitalertrag of 0,00 EUR (nothing was booked).
        nettos = list(NETTO_RE.finditer(self.compact))
        if len(nettos) > 1:
            raise ParseError(0, f"{self.filename}: {len(nettos)} × Netto")
        m_netto = nettos[0] if nettos else None
        if m_netto:
            direction, netto = m_netto.group(1), _dec(m_netto.group(2))
        else:
            kap = self._require(KAPITALERTRAG_RE, "Netto or Kapitalertrag")
            if _dec(kap.group(1)) != 0:
                raise ParseError(
                    0,
                    f"{self.filename}: Vorabpauschale with Kapitalertrag but "
                    "no Netto line",
                )
            direction, netto = "", Decimal(0)
        doc_date = _date(self._require(DOC_DATE_RE, "Datum").group(1))
        qty, unit = self._bestand()
        if netto != 0 and direction != "zulasten":
            raise ParseError(
                0,
                f"{self.filename}: Vorabpauschale credited (Netto zugunsten) — "
                "only debited tax is supported",
            )
        schluss_date = self._require(SCHLUSSTAG_RE, "Schlusstag").group(1)
        if netto == 0:
            logger.info(
                "Vorabpauschale %s: no tax debited (covered by Freistellungsauftrag) "
                "— 0 transactions",
                isin,
            )
            return None
        valuta = VALUTA_RE.search(self.compact)
        line = InvestStatementLine(
            id=_hash_id(
                "VORAB",
                isin,
                schluss_date,
                qty,
            ),
            date=_date(valuta.group(1)) if valuta else doc_date,
            memo=f"Steuer Vorabpauschale {name} ({wkn}), Bestand {qty} {unit}",
            trntype="INVEXPENSE",
            security_id=isin,
            amount=-netto,
        )
        return line
