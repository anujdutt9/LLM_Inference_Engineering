import datetime as dt
from decimal import Decimal

from ledger.invoices import Invoice, InvoiceLine, Numbering, due_date, totals_by_currency


def test_numbers_are_zero_padded_and_sequential():
    n = Numbering()
    assert [n.next(dt.date(2026, 3, 1)) for _ in range(3)] == ["INV-2026-00001", "INV-2026-00002", "INV-2026-00003"]


def test_numbering_restarts_each_year():
    n = Numbering()
    assert n.next(dt.date(2026, 12, 31)) == "INV-2026-00001"
    assert n.next(dt.date(2026, 12, 31)) == "INV-2026-00002"
    assert n.next(dt.date(2027, 1, 1)) == "INV-2027-00001"


def test_due_date_on_a_weekday_stays():
    # 2026-06-01 (Mon) + 30 = 2026-07-01 (Wed)
    assert due_date(dt.date(2026, 6, 1), 30) == dt.date(2026, 7, 1)


def test_due_date_on_a_saturday_moves_to_monday():
    # 2026-06-05 (Fri) + 30 = 2026-07-05 is a Sunday; 2026-06-04 + 30 = 2026-07-04 is a Saturday
    assert due_date(dt.date(2026, 6, 4), 30) == dt.date(2026, 7, 6)


def test_due_date_on_a_sunday_moves_to_monday():
    assert due_date(dt.date(2026, 6, 5), 30) == dt.date(2026, 7, 6)


def test_net_zero_is_due_on_issue_day():
    assert due_date(dt.date(2026, 6, 3), 0) == dt.date(2026, 6, 3)


def test_totals_are_exact():
    lines = [InvoiceLine("x", Decimal("0.10")) for _ in range(3)]
    assert totals_by_currency(lines) == {"USD": Decimal("0.30")}


def test_totals_per_currency_whatever_the_case():
    lines = [
        InvoiceLine("a", Decimal("10.00"), "usd"),
        InvoiceLine("b", Decimal("5.50"), "EUR"),
        InvoiceLine("c", Decimal("2.25"), "USD"),
    ]
    assert totals_by_currency(lines) == {"USD": Decimal("12.25"), "EUR": Decimal("5.50")}


def test_credit_note():
    inv = Invoice("INV-2026-00009", dt.date(2026, 5, 1), lines=[InvoiceLine("refund", Decimal("-40.00"))])
    assert inv.is_credit_note


def test_invoice_due_and_totals():
    inv = Invoice("INV-2026-00010", dt.date(2026, 6, 1), net_days=14, lines=[InvoiceLine("API", Decimal("12.34"))])
    assert inv.due == dt.date(2026, 6, 15)
    assert inv.totals() == {"USD": Decimal("12.34")}
    assert not inv.is_credit_note


# ---- payments and status
def test_partial_payment_then_paid():
    inv = Invoice("INV-2026-00011", dt.date(2026, 6, 1), lines=[InvoiceLine("API", Decimal("100.00"))])
    assert inv.status(dt.date(2026, 6, 2)) == "open"
    inv.pay(Decimal("40.00"), dt.date(2026, 6, 10))
    assert inv.balance() == Decimal("60.00")
    assert inv.status(dt.date(2026, 6, 10)) == "partial"
    inv.pay(Decimal("60.00"), dt.date(2026, 6, 20))
    assert inv.status(dt.date(2026, 6, 20)) == "paid"


def test_overdue_after_the_due_date():
    inv = Invoice("INV-2026-00012", dt.date(2026, 6, 1), lines=[InvoiceLine("API", Decimal("100.00"))])
    assert inv.status(dt.date(2026, 7, 2)) == "overdue"
