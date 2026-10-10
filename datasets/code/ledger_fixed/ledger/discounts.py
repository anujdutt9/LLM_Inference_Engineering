"""Coupons and how they stack.

A coupon is either a percentage off or a fixed amount off. When a customer has several,
all percentages apply first (each to the running amount), then the fixed amounts, so a
fixed coupon never gets multiplied. A coupon is valid up to and including its expiry
date, and only on orders of at least its minimum spend. However many coupons stack, the
total discount never exceeds half of the original amount. The same code given twice counts
once (codes are case-insensitive: "SAVE10" and "save10" are the same coupon).

Account credits are separate from coupons and apply after them, to what is left to pay: the
credit that expires first is used first, expired credits are skipped (a credit is usable on
its expiry day), a credit can be partly used (the rest stays for next time), and the amount
due never goes below zero.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
MAX_DISCOUNT_SHARE = Decimal("0.5")


@dataclass(frozen=True)
class Coupon:
    code: str
    percent: Decimal | None = None  # e.g. Decimal("10") for 10% off
    fixed: Decimal | None = None  # e.g. Decimal("25.00") off
    expires: dt.date | None = None  # last day it can be used
    min_spend: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if (self.percent is None) == (self.fixed is None):
            raise ValueError("a coupon is either a percentage or a fixed amount")


def is_valid(coupon: Coupon, amount: Decimal, on: dt.date) -> bool:
    """Not expired (the expiry day itself still counts) and the order meets the minimum spend."""
    if coupon.expires is not None and on > coupon.expires:
        return False
    return amount >= coupon.min_spend


def apply_coupons(amount: Decimal, coupons: list[Coupon], on: dt.date) -> Decimal:
    """The amount after all valid coupons: percentages first, then fixed amounts, with the
    total discount capped at half the original amount. Rounded to the cent."""
    seen: set[str] = set()
    unique = []
    for c in coupons:
        if c.code.lower() not in seen:
            seen.add(c.code.lower())
            unique.append(c)
    valid = [c for c in unique if is_valid(c, amount, on)]
    result = amount
    for c in (c for c in valid if c.percent is not None):
        result -= result * c.percent / 100
    for c in (c for c in valid if c.fixed is not None):
        result -= c.fixed
    floor = amount * (1 - MAX_DISCOUNT_SHARE)
    return max(result, floor).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Credit:
    amount: Decimal
    expires: dt.date


def apply_credits(due: Decimal, credits: list[Credit], on: dt.date) -> tuple[Decimal, list[Credit]]:
    """(amount still due, the credits left afterwards): soonest-expiring first, expired ones
    skipped (and dropped), partly used ones kept with their remainder, due never below zero."""
    left: list[Credit] = []
    for credit in sorted(credits, key=lambda c: c.expires):
        if on > credit.expires:
            continue
        used = min(credit.amount, due)
        due -= used
        if credit.amount - used > 0:
            left.append(Credit(credit.amount - used, credit.expires))
    return due, left


def discount_amount(amount: Decimal, coupons: list[Coupon], on: dt.date) -> Decimal:
    """How much the coupons take off `amount`."""
    return (amount - apply_coupons(amount, coupons, on)).quantize(CENT, rounding=ROUND_HALF_UP)
