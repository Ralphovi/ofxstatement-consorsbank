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
"""Tests for depot documents (Orderabrechnung, Ertragsgutschrift, Vorabpauschale).

The synthetic texts mirror pdfplumber's extraction of real documents,
including the dropped spaces and (cid:NNN) glyph placeholders.
"""

from datetime import datetime
from decimal import Decimal

import pytest
from ofxstatement.exceptions import ParseError
from ofxstatement.ofx import OfxWriter

from ofxstatement_consorsbank.depot import DepotDocument, detect_depot_document

ORDER_NEW = """\
Depotnummer 0000012345
ORDERABRECHNUNG
KAUF AM08.09.2023 UM09:30:21 SPARPLAN NR.274766086.001
Bezeichnung WKN ISIN
XTR.EUROSTOXX501D DBX1EU LU0274211217
Einheit Umsatz
ST 6,77371
PreisproAnteil 43,815000EUR NETTO
Kurswert 296,79EUR
zulastenKonto-Nr.0000099999 296,79EUR
Valuta12.09.2023
"""

ORDER_OLD_WITH_FEE = """\
Depotnummer 0000012345
ORDERABRECHNUNG
KAUF AM30.03.2020 UM21:03:17 TRADEGATEEXCHANGE NR.161236638.001
Bezeichnung WKN ISIN
XTR.EUROSTOXX501D DBX1EU LU0274211217
Einheit Umsatz
ST 134,00000
Kurs 29,710000EUR P.ST. FRANCOCOURTAGE
Kurswert EUR 3.981,14
Grundgeb(cid:252)hr EUR 3,95
Wert01.04.2020 EUR 3.985,09
zulastenKonto-Nr.0000099999
"""

# Account number directly followed by an amount without thousands separator
# must not be read as "...4911" + "6,00".
ORDER_GLUED_AMOUNT = """\
Depotnummer 0000012345
ORDERABRECHNUNG
KAUF AM01.02.2023 UM09:30:00 SPARPLAN NR.264286795.001
Bezeichnung WKN ISIN
XTR.EUROSTOXX501D DBX1EU LU0274211217
Einheit Umsatz
ST 2,57715
PreisproAnteil 45,010900EUR NETTO
Kurswert 116,00EUR
zulastenKonto-Nr.0000099999116,00EUR
"""

SELL = """\
Depotnummer 0000012345
ORDERABRECHNUNG
VERKAUF AM21.11.2024 UM17:11:12 TRADEGATEEXCHANGE NR.314304934.001
Bezeichnung WKN ISIN
XTR.EUROSTOXX501D DBX1EU LU0274211217
Einheit Umsatz
ST 136,00000
PreisproAnteil 49,125000EUR FRANCOCOURTAGE
Kurswert 6.681,00EUR
abzgl.Handelsplatzkosten 0,95EUR
abzgl.Provision 16,70EUR
abzgl.Grundgeb(cid:252)hr 4,95EUR
zugunstenKonto-Nr.0000099999 6.658,40EUR
Valuta25.11.2024
"""

BOND = """\
Depotnummer 0000012345
ORDERABRECHNUNG
KAUF AM28.05.2025 UM07:30:10 TRADEGATEEXCHANGE NR.334553501.001
Bezeichnung WKN ISIN
4,75%BUNDANL.V.03/34 04.JULI 113522 DE0001135226
Einheit Umsatz F(cid:228)lligkeit
EUR 1.000,00000 LetzteF(cid:228)lligkeit 04.07.2034
PreisproAnteil 118,659000 % FRANCOCOURTAGE
Kurswert 1.186,59EUR
St(cid:252)ckzinsZinsvaluta29.05.2025330Tage 42,95EUR
Handelsplatzkosten 0,95EUR
Provision 5,00EUR
Grundgeb(cid:252)hr 4,95EUR
zulastenKonto-Nr.0000099999 1.240,44EUR
Valuta30.05.2025
"""

ORDER_ZERO_BOOKED = ORDER_NEW.replace(
    "zulastenKonto-Nr.0000099999 296,79EUR", "zulastenKonto-Nr.0000099999 0,00EUR"
)

DIVIDEND = """\
Depotnummer: 0000012345
Datum: 07.06.2023
Seite: 1von2
Ertragsgutschrift
Wertpapierbezeichnung WKN ISIN
XtrackersEuroStoxx50Inhaber-Anteile1Do.N. DBX1EU LU0274211217
Bestand
318,19647St(cid:252)ck
Ertragsaussch(cid:252)ttungjeAnteil0,1761EUR Schlusstag23.05.2023
Brutto 56,03EUR
Nettozugunsten IBAN DE00760300800000099999 50,00EUR
Valuta07.06.2023 BIC CSDBDE71XXX
"""

INTEREST = """\
Depotnummer: 0000012345
Datum: 07.07.2025
Seite: 1von2
Zinsgutschrift
Wertpapierbezeichnung WKN ISIN
Bundesrep.DeutschlandAnl.v.2003(2034) 113522 DE0001135226
Bestand Kupontag
1.000EUR 04.07.2025
Zinssatz4,75% Schlusstag01.07.2025
Brutto 47,50EUR
Nettozugunsten IBAN DE00760300800000099999 47,50EUR
Valuta04.07.2025 BIC CSDBDE71XXX
"""

VORAB_ZERO = """\
Depotnummer: 0000012345
Datum: 21.02.2024
Seite: 1von2
Vorabpauschale
Wertpapierbezeichnung WKN ISIN
InvescoM.ISTOXXEurop600ETFRegisteredSharesAcco.N. A0RGCK IE00B60SWW18
Bestand
6,68822St(cid:252)ck
VorabpauschaleproAnteil1,78594069EUR Schlusstag01.01.2024
Kapitalertrag 0,00EUR
Nettozugunsten IBAN DE00760300800000099999 0,00EUR
Valuta BIC CSDBDE71XXX
"""

# 2020-era layout: no Netto line at all.
VORAB_2020 = """\
Depotnummer: 0000012345
Datum: 20.01.2020 Seite: 1von2
Vorabpauschale
Wertpapierbezeichnung WKN ISIN
XtrackersEuroStoxx50Inhaber-Anteile1Do.N. DBX1EU LU0274211217
Bestand
11,58923St(cid:252)ck
VorabpauschaleproAnteil0,00EUR Schlusstag01.01.2020
Kapitalertrag 0,00EUR
"""

VORAB_TAXED = VORAB_ZERO.replace(
    "Nettozugunsten IBAN DE00760300800000099999 0,00EUR",
    "Nettozulasten IBAN DE00760300800000099999 1,23EUR",
).replace("Valuta BIC", "Valuta05.01.2024 BIC")


def _parse(text: str):
    lines = text.splitlines()
    kind = detect_depot_document(lines)
    assert kind is not None
    return kind, DepotDocument(lines, "test.pdf").parse(kind)


def test_detect_ignores_bank_statements():
    lines = ["Kontoauszug 1 Konto-Nr. 0000099999 Blatt 1 / 2", "Kontotyp Girokonto"]
    assert detect_depot_document(lines) is None


def test_buy_new_layout():
    kind, stmt = _parse(ORDER_NEW)
    assert kind == "order"
    assert stmt.account_id == "0000012345"
    (line,) = stmt.invest_lines
    assert line.id == "ord274766086.001"
    assert line.trntype == "BUYSTOCK" and line.trntype_detailed == "BUY"
    assert line.security_id == "LU0274211217"
    assert line.units == Decimal("6.77371")
    assert line.unit_price == Decimal("43.815000")
    assert line.amount == Decimal("-296.79")
    assert line.fees is None
    assert line.date == datetime(2023, 9, 8, 9, 30, 21)


def test_buy_old_layout_with_fee():
    _, stmt = _parse(ORDER_OLD_WITH_FEE)
    (line,) = stmt.invest_lines
    assert line.amount == Decimal("-3985.09")
    assert line.fees == Decimal("3.95")
    assert line.units == Decimal("134.00000")
    assert line.unit_price == Decimal("29.710000")


def test_total_glued_to_account_number():
    _, stmt = _parse(ORDER_GLUED_AMOUNT)
    assert stmt.invest_lines[0].amount == Decimal("-116.00")


def test_sell():
    _, stmt = _parse(SELL)
    (line,) = stmt.invest_lines
    assert line.trntype == "SELLSTOCK" and line.trntype_detailed == "SELL"
    assert line.units == Decimal("-136.00000")
    assert line.amount == Decimal("6658.40")
    assert line.fees == Decimal("22.60")


def test_bond_buy():
    _, stmt = _parse(BOND)
    (line,) = stmt.invest_lines
    # ofxstatement's writer can't express debt securities correctly.
    assert line.trntype == "BUYSTOCK" and line.trntype_detailed == "BUY"
    assert "Anleihe" in line.memo
    assert line.security_id == "DE0001135226"
    assert line.units == Decimal("1000.00000")
    assert line.unit_price == Decimal("1.18659")
    assert line.amount == Decimal("-1240.44")
    assert line.fees == Decimal("10.90")  # Stückzins is not a fee
    assert "Stückzins 42.95" in line.memo


def test_zero_booked_order_yields_no_transaction():
    # The "KAUF - KORREKTUR" with the real debit is the only transaction.
    kind, stmt = _parse(ORDER_ZERO_BOOKED)
    assert kind == "order"
    assert stmt.invest_lines == []


def test_dividend():
    kind, stmt = _parse(DIVIDEND)
    assert kind == "income"
    (line,) = stmt.invest_lines
    assert line.trntype == "INCOME" and line.trntype_detailed == "DIV"
    assert line.amount == Decimal("50.00")
    assert line.fees == Decimal("6.03")  # withheld = brutto - netto
    assert line.date == datetime(2023, 6, 7)


def test_dividend_id_distinguishes_custody_positions():
    _, a = _parse(DIVIDEND)
    _, b = _parse(DIVIDEND.replace("318,19647", "81,97666"))
    _, c = _parse(DIVIDEND.replace("50,00EUR", "51,00EUR"))  # Neuabrechnung
    assert a.invest_lines[0].id != b.invest_lines[0].id
    assert a.invest_lines[0].id == c.invest_lines[0].id


def test_interest():
    _, stmt = _parse(INTEREST)
    (line,) = stmt.invest_lines
    assert line.trntype_detailed == "INTEREST"
    assert line.amount == Decimal("47.50")
    assert line.fees is None


def test_vorabpauschale_without_tax_yields_nothing():
    for text in (VORAB_ZERO, VORAB_2020):
        kind, stmt = _parse(text)
        assert kind == "vorab"
        assert stmt.invest_lines == []


def test_vorabpauschale_with_tax():
    _, stmt = _parse(VORAB_TAXED)
    (line,) = stmt.invest_lines
    assert line.trntype == "INVEXPENSE"
    assert line.amount == Decimal("-1.23")
    assert line.date == datetime(2024, 1, 5)


def test_ofx_output_contains_invest_transactions():
    _, stmt = _parse(BOND)
    ofx = OfxWriter(stmt).toxml()
    assert "<BUYSTOCK>" in ofx and "DEBT" not in ofx
    assert "<UNIQUEID>DE0001135226</UNIQUEID>" in ofx
    assert "<ACCTID>0000012345</ACCTID>" in ofx


def test_correction_with_plain_header_keeps_order_fitid():
    # Real "KAUF - KORREKTUR" PDFs (2024) print the same "KAUF AM" header as
    # the original and only differ in the booked total.
    _, orig = _parse(ORDER_ZERO_BOOKED)
    _, corr = _parse(ORDER_NEW)
    assert orig.invest_lines == []
    assert corr.invest_lines[0].id == "ord274766086.001"
    assert corr.invest_lines[0].amount == Decimal("-296.79")


def test_correction_marker_in_header():
    text = ORDER_NEW.replace("KAUF AM08.09.2023", "KAUF - KORREKTUR AM08.09.2023")
    _, stmt = _parse(text)
    (line,) = stmt.invest_lines
    assert line.id == "ord274766086.001"
    assert line.trntype == "BUYSTOCK"
    assert line.amount == Decimal("-296.79")


def test_interest_id_uses_integer_nominal():
    _, a = _parse(INTEREST)
    _, b = _parse(INTEREST.replace("1.000EUR 04.07.2025", "2.000EUR 04.07.2025"))
    assert a.invest_lines[0].id != b.invest_lines[0].id
    assert "Bestand 1000 EUR" in a.invest_lines[0].memo


def test_dividend_id_uses_whole_share_holdings():
    # 2022 statements print whole holdings without decimals ("143Stück").
    _, a = _parse(DIVIDEND.replace("318,19647", "143"))
    _, b = _parse(DIVIDEND.replace("318,19647", "144"))
    assert a.invest_lines[0].id != b.invest_lines[0].id
    assert "Bestand 143 Stück" in a.invest_lines[0].memo


def test_bestand_is_canonical_in_ids():
    _, a = _parse(DIVIDEND.replace("318,19647", "143"))
    _, b = _parse(DIVIDEND.replace("318,19647", "143,00000"))
    assert a.invest_lines[0].id == b.invest_lines[0].id


def test_income_without_bestand_is_rejected():
    with pytest.raises(ParseError, match="Bestand"):
        _parse(DIVIDEND.replace("318,19647St(cid:252)ck", ""))


def test_vorabpauschale_id_distinguishes_custody_positions():
    _, a = _parse(VORAB_TAXED)
    _, b = _parse(VORAB_TAXED.replace("6,68822", "3,00000"))
    assert a.invest_lines[0].id != b.invest_lines[0].id


def test_sale_with_tax_deduction_is_rejected():
    taxed = SELL.replace(
        "zugunstenKonto-Nr.0000099999 6.658,40EUR",
        "Kapitalertragsteuer 100,00EUR\nzugunstenKonto-Nr.0000099999 6.558,40EUR",
    )
    with pytest.raises(ParseError, match="unrecognised deduction"):
        _parse(taxed)


def test_fees_come_from_fee_lines():
    _, stmt = _parse(BOND)
    assert stmt.invest_lines[0].fees == Decimal("10.90")
    _, stmt = _parse(ORDER_OLD_WITH_FEE)
    assert stmt.invest_lines[0].fees == Decimal("3.95")


@pytest.mark.parametrize(
    "text, old, new",
    [
        (ORDER_NEW, "zulastenKonto", "zugunstenKonto"),
        (SELL, "zugunstenKonto", "zulastenKonto"),
    ],
)
def test_booking_direction_must_match_side(text, old, new):
    with pytest.raises(ParseError, match="direction"):
        _parse(text.replace(old, new))


def test_missing_depot_number_is_rejected():
    with pytest.raises(ParseError, match="Depotnummer"):
        _parse(ORDER_NEW.replace("Depotnummer 0000012345\n", ""))


@pytest.mark.parametrize(
    "text, old, new",
    [
        (ORDER_NEW, "43,815000EUR", "43,815000USD"),
        (DIVIDEND, "Brutto 56,03EUR", "Brutto 60,00USD\nBrutto 56,03EUR"),
    ],
)
def test_foreign_currency_documents_are_rejected(text, old, new):
    with pytest.raises(ParseError, match="USD"):
        _parse(text.replace(old, new))


def test_repeated_header_prefixes_stay_fast():
    import time

    junk = "ORDERABRECHNUNG\nKAUF AM08.09.2023 UM09:30:21 " * 20000
    lines = (junk + ORDER_NEW).splitlines()
    start = time.perf_counter()
    DepotDocument(lines, "test.pdf").parse("order")
    assert time.perf_counter() - start < 2


def test_units_must_match_kurswert():
    with pytest.raises(ParseError, match="does not match Kurswert"):
        _parse(ORDER_NEW.replace("ST 6,77371", "ST 7,77371"))


def test_vorabpauschale_without_netto_needs_zero_kapitalertrag():
    with pytest.raises(ParseError, match="Kapitalertrag"):
        _parse(VORAB_2020.replace("Kapitalertrag 0,00EUR", "Kapitalertrag 5,00EUR"))
    with pytest.raises(ParseError, match="Netto or Kapitalertrag"):
        _parse(VORAB_2020.replace("Kapitalertrag 0,00EUR", ""))


def test_long_quantities_are_not_rounded_in_ids():
    a = "1," + "1" * 40
    b = "1," + "1" * 39 + "2"
    _, x = _parse(DIVIDEND.replace("318,19647", a))
    _, y = _parse(DIVIDEND.replace("318,19647", b))
    assert x.invest_lines[0].id != y.invest_lines[0].id


def test_invalid_values_raise_parse_error():
    with pytest.raises(ParseError):
        _parse(ORDER_NEW.replace("UM09:30:21", "UM29:30:21"))
    with pytest.raises(ParseError):
        _parse(DIVIDEND.replace("50,00EUR", "0,00EUR").replace("56,03", "0,00"))


def test_logs_carry_no_amounts(caplog):
    with caplog.at_level("DEBUG"):
        _parse(SELL)
        _parse(DIVIDEND)
    text = " ".join(r.getMessage() for r in caplog.records)
    for amount in ("6658", "6681", "136", "49,125", "49.125", "56.03", "50.00"):
        assert amount not in text


def test_currency_code_before_amount_is_rejected():
    with pytest.raises(ParseError, match="USD"):
        _parse(DIVIDEND.replace("Brutto 56,03EUR", "USD 60,00\nBrutto 56,03EUR"))


def test_zero_booked_order_is_still_validated():
    with pytest.raises(ParseError, match="Kurswert"):
        _parse(ORDER_ZERO_BOOKED.replace("ST 6,77371", "ST 7,77371"))
    with pytest.raises(ParseError, match="invalid date"):
        _parse(ORDER_ZERO_BOOKED.replace("UM09:30:21", "UM29:30:21"))


def test_untaxed_vorabpauschale_still_needs_bestand():
    with pytest.raises(ParseError, match="Bestand"):
        _parse(VORAB_ZERO.replace("6,68822St(cid:252)ck", ""))


def test_income_ids_differ_by_kind_and_gross():
    _, a = _parse(DIVIDEND)
    _, b = _parse(DIVIDEND.replace("Brutto 56,03EUR", "Brutto 57,03EUR"))
    _, c = _parse(DIVIDEND.replace("Ertragsgutschrift", "Zinsgutschrift"))
    ids = {x.invest_lines[0].id for x in (a, b, c)}
    assert len(ids) == 3


def test_income_booked_zulasten_is_rejected():
    with pytest.raises(ParseError, match="zulasten"):
        _parse(DIVIDEND.replace("Nettozugunsten", "Nettozulasten"))


def test_depot_header_after_line_80_is_detected():
    lines = ["Kontoauszug padding"] * 100 + ORDER_NEW.splitlines()
    assert detect_depot_document(lines) == "order"


def test_values_that_ofx_would_round_are_rejected():
    with pytest.raises(ParseError, match="5 decimals"):
        _parse(ORDER_NEW.replace("ST 6,77371", "ST 6,773711"))
    with pytest.raises(ParseError, match="5 decimals"):
        _parse(ORDER_NEW.replace("43,815000EUR", "43,815004EUR"))


def test_schlusstag_is_required_for_income_and_vorab():
    with pytest.raises(ParseError, match="Schlusstag"):
        _parse(DIVIDEND.replace("Schlusstag23.05.2023", ""))
    with pytest.raises(ParseError, match="Schlusstag"):
        _parse(VORAB_TAXED.replace("Schlusstag01.01.2024", ""))


def test_credited_vorabpauschale_is_rejected():
    text = VORAB_ZERO.replace(
        "Nettozugunsten IBAN DE00760300800000099999 0,00EUR",
        "Nettozugunsten IBAN DE00760300800000099999 1,23EUR",
    )
    with pytest.raises(ParseError, match="credited"):
        _parse(text)
