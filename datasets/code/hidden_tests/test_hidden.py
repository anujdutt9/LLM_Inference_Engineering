"""Hidden checks, one group per planted bug, with values the visible tests don't use: a fix that
only special-cases the visible tests fails these. Run against a fixed copy (analysis/grade_code.py)."""

import datetime as dt
from decimal import Decimal

import pytest

from ledger import discounts, invoices, pricing, tax


# P1 tier boundary: tiers include their last unit
def test_P1_boundary_scale():
    assert pricing.usage_charge(pricing.SCALE, 10_000) == Decimal("80.000")
    assert pricing.usage_charge(pricing.SCALE, 100_000) == Decimal("80.000") + Decimal("540.000")


def test_P1_one_past_the_boundary():
    assert pricing.usage_charge(pricing.STARTER, 1_001) == Decimal("10.008")


# P2 proration by the month's real length
@pytest.mark.parametrize(("month", "days"), [(2, 28), (4, 30), (8, 31)])
def test_P2_one_day_is_one_over_month_length(month, days):
    fee = pricing.prorated_base_fee(pricing.SCALE, dt.date(2026, month, 1), dt.date(2026, month, days))
    expected = (Decimal("499.00") / days).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    assert fee == expected


def test_P2_leap_february():
    assert pricing.prorated_base_fee(pricing.STARTER, dt.date(2028, 2, 1), dt.date(2028, 2, 15)) == Decimal("25.34")


# T1 half-up rounding of each line
def test_T1_half_up():
    # 10.00 x 7.25% = 0.725 -> 0.73 ; 14.00 x 7.25% = 1.015 -> 1.02
    ca = tax.Customer("US-CA")
    assert tax.line_tax(tax.Line("x", "usage", Decimal("10.00")), ca) == Decimal("0.73")
    assert tax.line_tax(tax.Line("x", "usage", Decimal("14.00")), ca) == Decimal("1.02")


# T2 tax on the discounted amount
def test_T2_discounted():
    line = tax.Line("x", "usage", Decimal("250.00"), discount=Decimal("100.00"))
    assert tax.line_tax(line, tax.Customer("GB")) == Decimal("30.00")


def test_T2_full_discount_pays_no_tax():
    line = tax.Line("x", "usage", Decimal("40.00"), discount=Decimal("40.00"))
    assert tax.line_tax(line, tax.Customer("FR")) == Decimal("0.00")


# D1 coupon valid through its expiry day
def test_D1_expiry_day_with_fixed_coupon():
    day = dt.date(2026, 1, 31)
    c = discounts.Coupon("F", fixed=Decimal("5.00"), expires=day)
    assert discounts.apply_coupons(Decimal("50.00"), [c], day) == Decimal("45.00")
    assert discounts.apply_coupons(Decimal("50.00"), [c], day + dt.timedelta(days=1)) == Decimal("50.00")


# D2 all percentages before fixed amounts, whatever the list order
def test_D2_order_independent():
    coupons = [
        discounts.Coupon("F1", fixed=Decimal("5.00")),
        discounts.Coupon("P1", percent=Decimal("20")),
        discounts.Coupon("F2", fixed=Decimal("3.00")),
        discounts.Coupon("P2", percent=Decimal("10")),
    ]
    on = dt.date(2026, 6, 1)
    # 200 -> 160 -> 144 -> 139 -> 136
    assert discounts.apply_coupons(Decimal("200.00"), coupons, on) == Decimal("136.00")
    assert discounts.apply_coupons(Decimal("200.00"), list(reversed(coupons)), on) == Decimal("136.00")


# I1 Saturday due dates move to Monday
def test_I1_saturday():
    # 2026-10-03 is a Saturday
    assert invoices.due_date(dt.date(2026, 9, 3), 30) == dt.date(2026, 10, 5)
    assert invoices.due_date(dt.date(2026, 10, 3), 0) == dt.date(2026, 10, 5)


# I2 the sequence restarts each year, and years don't share it
def test_I2_years_independent():
    n = invoices.Numbering()
    for _ in range(4):
        n.next(dt.date(2026, 5, 1))
    assert n.next(dt.date(2027, 2, 1)) == "INV-2027-00001"
    assert n.next(dt.date(2026, 12, 1)) == "INV-2026-00005"
    assert n.next(dt.date(2027, 3, 1)) == "INV-2027-00002"


# P3 (no visible test) not active that month: no base fee, never a negative one
def test_P3_cancelled_before_the_month():
    assert pricing.active_days(dt.date(2026, 5, 1), dt.date(2026, 1, 1), dt.date(2026, 4, 20)) == 0
    assert pricing.prorated_base_fee(pricing.SCALE, dt.date(2026, 5, 1), dt.date(2026, 1, 1), dt.date(2026, 4, 20)) == Decimal("0.00")


def test_P3_starting_after_the_month():
    assert pricing.active_days(dt.date(2026, 5, 1), dt.date(2026, 6, 3), None) == 0


# P4 (feature) upgrade_charge
def test_P4_change_on_the_first_is_the_whole_difference():
    assert pricing.upgrade_charge(pricing.STARTER, pricing.SCALE, dt.date(2026, 7, 1)) == Decimal("450.00")


def test_P4_change_on_the_last_day_is_one_day():
    # Feb 28 2026: 1 of 28 days; 499/28 = 17.82, 49/28 = 1.75
    assert pricing.upgrade_charge(pricing.STARTER, pricing.SCALE, dt.date(2026, 2, 28)) == Decimal("16.07")


def test_P4_each_side_rounded_half_up():
    # Aug 17 2026: 15 of 31 days; 499 x 15/31 = 241.451... -> 241.45 ; 49 x 15/31 = 23.709... -> 23.71
    assert pricing.upgrade_charge(pricing.STARTER, pricing.SCALE, dt.date(2026, 8, 17)) == Decimal("217.74")


# T3 (no visible test) a blank VAT id is no VAT id
@pytest.mark.parametrize("vat_id", ["", "   "])
def test_T3_blank_vat_id_pays_vat(vat_id):
    customer = tax.Customer("DE", vat_id=vat_id)
    assert not tax.reverse_charge(customer)
    assert tax.line_tax(tax.Line("x", "usage", Decimal("100.00")), customer) == Decimal("19.00")


# T4 (feature) vat_breakdown
def test_T4_reverse_charge_is_a_zero_row():
    lines = [tax.Line("a", "usage", Decimal("100.00"), discount=Decimal("25.00"))]
    assert tax.vat_breakdown(lines, tax.Customer("FR", vat_id="FR1")) == [(Decimal("0"), Decimal("75.00"), Decimal("0.00"))]


def test_T4_tax_is_the_sum_of_rounded_lines():
    lines = [tax.Line("a", "usage", Decimal("2.00")), tax.Line("b", "usage", Decimal("2.00"))]
    assert tax.vat_breakdown(lines, tax.Customer("US-CA")) == [(Decimal("0.0725"), Decimal("4.00"), Decimal("0.30"))]


def test_T4_empty_invoice():
    assert tax.vat_breakdown([], tax.Customer("DE")) == []


# D3 (no visible test) a code given twice counts once, case-insensitively
def test_D3_duplicate_codes():
    on = dt.date(2026, 6, 1)
    coupons = [discounts.Coupon("SAVE10", percent=Decimal("10")), discounts.Coupon("save10", percent=Decimal("10"))]
    assert discounts.apply_coupons(Decimal("100.00"), coupons, on) == Decimal("90.00")


def test_D3_different_codes_both_apply():
    on = dt.date(2026, 6, 1)
    coupons = [discounts.Coupon("A5", fixed=Decimal("5.00")), discounts.Coupon("B5", fixed=Decimal("5.00"))]
    assert discounts.apply_coupons(Decimal("100.00"), coupons, on) == Decimal("90.00")


# D4 (feature) apply_credits
def test_D4_expired_credit_skipped_and_dropped():
    on = dt.date(2026, 6, 15)
    credits = [discounts.Credit(Decimal("50.00"), dt.date(2026, 6, 14)), discounts.Credit(Decimal("10.00"), dt.date(2026, 6, 15))]
    due, left = discounts.apply_credits(Decimal("30.00"), credits, on)
    assert due == Decimal("20.00") and left == []


def test_D4_credits_used_in_expiry_order_across_several():
    on = dt.date(2026, 1, 1)
    credits = [
        discounts.Credit(Decimal("10.00"), dt.date(2026, 3, 1)),
        discounts.Credit(Decimal("10.00"), dt.date(2026, 1, 31)),
        discounts.Credit(Decimal("10.00"), dt.date(2026, 2, 1)),
    ]
    due, left = discounts.apply_credits(Decimal("15.00"), credits, on)
    assert due == Decimal("0.00")
    assert left == [discounts.Credit(Decimal("5.00"), dt.date(2026, 2, 1)), discounts.Credit(Decimal("10.00"), dt.date(2026, 3, 1))]


def test_D4_no_credits():
    assert discounts.apply_credits(Decimal("12.34"), [], dt.date(2026, 1, 1)) == (Decimal("12.34"), [])


# I3 (no visible test) negative payment terms are an error
def test_I3_negative_net_days():
    with pytest.raises(ValueError):
        invoices.due_date(dt.date(2026, 6, 1), -1)


# I4 (feature) payments and status
def _inv(*lines):
    return invoices.Invoice("INV-2026-00099", dt.date(2026, 6, 1), lines=list(lines))


def test_I4_overpayment_is_paid():
    inv = _inv(invoices.InvoiceLine("x", Decimal("50.00")))
    inv.pay(Decimal("60.00"), dt.date(2026, 6, 2))
    assert inv.balance() == Decimal("-10.00") and inv.status(dt.date(2026, 9, 1)) == "paid"


def test_I4_partial_then_overdue():
    inv = _inv(invoices.InvoiceLine("x", Decimal("50.00")))
    inv.pay(Decimal("10.00"), dt.date(2026, 6, 2))
    assert inv.status(dt.date(2026, 7, 1)) == "partial"  # due Jul 1: not yet overdue
    assert inv.status(dt.date(2026, 7, 2)) == "overdue"


def test_I4_multi_currency_rejects_payments():
    inv = _inv(invoices.InvoiceLine("a", Decimal("10.00"), "USD"), invoices.InvoiceLine("b", Decimal("10.00"), "EUR"))
    with pytest.raises(ValueError):
        inv.pay(Decimal("5.00"), dt.date(2026, 6, 2))


def test_I4_payment_must_be_positive():
    inv = _inv(invoices.InvoiceLine("x", Decimal("50.00")))
    with pytest.raises(ValueError):
        inv.pay(Decimal("0"), dt.date(2026, 6, 2))
