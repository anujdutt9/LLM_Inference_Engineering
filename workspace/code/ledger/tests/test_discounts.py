import datetime as dt
from decimal import Decimal

import pytest

from ledger.discounts import Coupon, apply_coupons, discount_amount, is_valid

TODAY = dt.date(2026, 6, 15)


def test_a_coupon_is_either_percent_or_fixed():
    with pytest.raises(ValueError):
        Coupon("BAD")
    with pytest.raises(ValueError):
        Coupon("BAD", percent=Decimal("10"), fixed=Decimal("5"))


def test_percentage_coupon():
    assert apply_coupons(Decimal("200.00"), [Coupon("TEN", percent=Decimal("10"))], TODAY) == Decimal("180.00")


def test_fixed_coupon():
    assert apply_coupons(Decimal("200.00"), [Coupon("FIVER", fixed=Decimal("5.00"))], TODAY) == Decimal("195.00")


def test_percentages_apply_before_fixed_amounts():
    coupons = [Coupon("TENOFF", fixed=Decimal("10.00")), Coupon("TEN", percent=Decimal("10"))]
    # 100 -> 10% off = 90 -> 10 off = 80 (fixed first would give (100 - 10) x 0.9 = 81)
    assert apply_coupons(Decimal("100.00"), coupons, TODAY) == Decimal("80.00")


def test_two_percentages_compound():
    coupons = [Coupon("A", percent=Decimal("10")), Coupon("B", percent=Decimal("20"))]
    assert apply_coupons(Decimal("100.00"), coupons, TODAY) == Decimal("72.00")


def test_a_coupon_is_valid_on_its_expiry_day():
    coupon = Coupon("LASTDAY", percent=Decimal("10"), expires=TODAY)
    assert is_valid(coupon, Decimal("100"), TODAY)
    assert apply_coupons(Decimal("100.00"), [coupon], TODAY) == Decimal("90.00")


def test_an_expired_coupon_is_ignored():
    coupon = Coupon("OLD", percent=Decimal("10"), expires=TODAY - dt.timedelta(days=1))
    assert apply_coupons(Decimal("100.00"), [coupon], TODAY) == Decimal("100.00")


def test_minimum_spend():
    coupon = Coupon("BIG", fixed=Decimal("20.00"), min_spend=Decimal("150.00"))
    assert apply_coupons(Decimal("149.99"), [coupon], TODAY) == Decimal("149.99")
    assert apply_coupons(Decimal("150.00"), [coupon], TODAY) == Decimal("130.00")


def test_total_discount_is_capped_at_half():
    coupons = [Coupon("A", percent=Decimal("40")), Coupon("B", fixed=Decimal("30.00"))]
    # 100 -> 60 -> 30, but never below 50
    assert apply_coupons(Decimal("100.00"), coupons, TODAY) == Decimal("50.00")


def test_discount_amount():
    assert discount_amount(Decimal("80.00"), [Coupon("A", percent=Decimal("25"))], TODAY) == Decimal("20.00")


def test_result_is_rounded_to_the_cent():
    assert apply_coupons(Decimal("10.00"), [Coupon("THIRD", percent=Decimal("33"))], TODAY) == Decimal("6.70")


# ---- account credits
from ledger.discounts import Credit, apply_credits  # noqa: E402


def test_credits_soonest_expiring_first():
    credits = [Credit(Decimal("30.00"), dt.date(2026, 12, 31)), Credit(Decimal("20.00"), dt.date(2026, 7, 1))]
    due, left = apply_credits(Decimal("35.00"), credits, TODAY)
    assert due == Decimal("0.00")
    assert left == [Credit(Decimal("15.00"), dt.date(2026, 12, 31))]


def test_credits_never_make_the_amount_negative():
    due, left = apply_credits(Decimal("10.00"), [Credit(Decimal("25.00"), dt.date(2026, 9, 1))], TODAY)
    assert due == Decimal("0.00")
    assert left == [Credit(Decimal("15.00"), dt.date(2026, 9, 1))]
