import datetime as dt
from decimal import Decimal

import pytest

from ledger.pricing import SCALE, STARTER, Plan, Tier, active_days, monthly_charge, prorated_base_fee, usage_charge


def test_usage_inside_the_first_tier():
    assert usage_charge(STARTER, 400) == Decimal("4.000")


def test_usage_exactly_at_a_tier_boundary_is_all_in_the_lower_tier():
    assert usage_charge(STARTER, 1_000) == Decimal("10.000")


def test_usage_across_two_tiers():
    assert usage_charge(STARTER, 1_500) == Decimal("14.000")


def test_usage_across_three_tiers():
    # 10,000 @ 0.008 + 90,000 @ 0.006 + 20,000 @ 0.004
    assert usage_charge(SCALE, 120_000) == Decimal("80.000") + Decimal("540.000") + Decimal("80.000")


def test_zero_usage_costs_nothing():
    assert usage_charge(SCALE, 0) == Decimal("0")


def test_negative_usage_is_rejected():
    with pytest.raises(ValueError):
        usage_charge(STARTER, -1)


def test_active_days_whole_month():
    assert active_days(dt.date(2026, 3, 1), dt.date(2026, 1, 10), None) == 31


def test_active_days_started_mid_month():
    assert active_days(dt.date(2026, 4, 1), dt.date(2026, 4, 21), None) == 10


def test_active_days_cancelled_mid_month():
    assert active_days(dt.date(2026, 4, 1), dt.date(2026, 3, 1), dt.date(2026, 4, 10)) == 10


def test_prorated_base_fee_uses_the_real_length_of_february():
    # active Feb 15-28: 14 of February's 28 days
    assert prorated_base_fee(STARTER, dt.date(2026, 2, 1), dt.date(2026, 2, 15)) == Decimal("24.50")


def test_prorated_base_fee_in_a_31_day_month():
    # active Jul 1-10: 10 of 31 days of 499.00
    assert prorated_base_fee(SCALE, dt.date(2026, 7, 1), dt.date(2026, 6, 1), dt.date(2026, 7, 10)) == Decimal("160.97")


def test_full_month_pays_the_whole_base_fee():
    assert prorated_base_fee(SCALE, dt.date(2026, 4, 1), dt.date(2025, 1, 1)) == Decimal("499.00")


def test_monthly_charge_never_below_the_minimum():
    # 3 active days of starter, no usage: 49 x 3/30 = 4.90, raised to the 49.00 minimum
    assert monthly_charge(STARTER, 0, dt.date(2026, 9, 1), dt.date(2026, 9, 28)) == Decimal("49.00")


def test_monthly_charge_adds_usage_to_the_prorated_fee():
    assert monthly_charge(SCALE, 12_000, dt.date(2026, 4, 1), dt.date(2025, 1, 1)) == Decimal("499.00") + Decimal(
        "80.00"
    ) + Decimal("12.00")


def test_a_single_unbounded_tier():
    flat = Plan("flat", Decimal("0"), (Tier(None, Decimal("0.5")),))
    assert usage_charge(flat, 7) == Decimal("3.5")


# ---- plan changes mid-month
from ledger.pricing import upgrade_charge  # noqa: E402


def test_upgrade_mid_month():
    # Apr 21: 10 of 30 days left; new 499.00 x 10/30 = 166.33, old 49.00 x 10/30 = 16.33
    assert upgrade_charge(STARTER, SCALE, dt.date(2026, 4, 21)) == Decimal("150.00")


def test_downgrade_is_a_credit():
    assert upgrade_charge(SCALE, STARTER, dt.date(2026, 4, 21)) == Decimal("-150.00")
