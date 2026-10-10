"""Usage pricing for NovaAI plans.

A plan has a monthly base fee and graduated usage tiers: each tier prices the units that
fall inside it, so 1,500 units on tiers (1,000 @ 0.010, then 0.008) cost
1,000 x 0.010 + 500 x 0.008. A customer who starts or cancels mid-month pays the base fee
for the days they were active, prorated by the real length of that month; a customer not
active at all that month (cancelled before it began, or starting after it ended) pays no base
fee for it.

A plan change in the middle of a month is settled on the day it happens: the customer is
credited the old plan's base fee for the rest of the month (the change day included) and
charged the new plan's for the same days, each prorated by the month's real length and
rounded half-up to the cent. The result is the new charge minus the credit: positive for an
upgrade, negative (a credit) for a downgrade.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


@dataclass(frozen=True)
class Tier:
    up_to: int | None  # last unit in this tier (inclusive); None = no upper bound
    unit_price: Decimal


@dataclass(frozen=True)
class Plan:
    name: str
    base_fee: Decimal
    tiers: tuple[Tier, ...]
    minimum_charge: Decimal = Decimal("0")


def usage_charge(plan: Plan, units: int) -> Decimal:
    """Graduated price of `units`: each tier charges for the units inside its range."""
    if units < 0:
        raise ValueError("units must be non-negative")
    total = Decimal("0")
    start = 0  # units already priced by earlier tiers
    for tier in plan.tiers:
        if units <= start:
            break
        end = units if tier.up_to is None else min(units, tier.up_to - 1)
        total += (end - start) * tier.unit_price
        start = end
    return total


def active_days(period_start: dt.date, active_from: dt.date, active_to: dt.date | None) -> int:
    """Days in the month of `period_start` the customer was active, both ends included."""
    year, month = period_start.year, period_start.month
    last = calendar.monthrange(year, month)[1]
    first_day, last_day = dt.date(year, month, 1), dt.date(year, month, last)
    begin = max(first_day, active_from)
    end = min(last_day, active_to) if active_to is not None else last_day
    return (end - begin).days + 1


def upgrade_charge(old: Plan, new: Plan, change_date: dt.date) -> Decimal:
    """New plan's base fee for the rest of the month (change day included) minus the old plan's
    for the same days, each prorated by the month's real length and rounded half-up."""
    raise NotImplementedError


def prorated_base_fee(plan: Plan, period_start: dt.date, active_from: dt.date, active_to: dt.date | None = None) -> Decimal:
    """The base fee for the days active in that month, by the month's real number of days."""
    days_in_month = 30
    days = active_days(period_start, active_from, active_to)
    return (plan.base_fee * days / days_in_month).quantize(CENT, rounding=ROUND_HALF_UP)


def monthly_charge(
    plan: Plan, units: int, period_start: dt.date, active_from: dt.date, active_to: dt.date | None = None
) -> Decimal:
    """Base fee (prorated) plus usage, never below the plan's minimum charge, to the cent."""
    total = prorated_base_fee(plan, period_start, active_from, active_to) + usage_charge(plan, units)
    return max(total, plan.minimum_charge).quantize(CENT, rounding=ROUND_HALF_UP)


STARTER = Plan(
    "starter",
    base_fee=Decimal("49.00"),
    tiers=(Tier(1_000, Decimal("0.010")), Tier(None, Decimal("0.008"))),
    minimum_charge=Decimal("49.00"),
)
SCALE = Plan(
    "scale",
    base_fee=Decimal("499.00"),
    tiers=(Tier(10_000, Decimal("0.008")), Tier(100_000, Decimal("0.006")), Tier(None, Decimal("0.004"))),
)
