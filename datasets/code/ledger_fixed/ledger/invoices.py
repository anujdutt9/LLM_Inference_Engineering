"""Invoice numbering, due dates and totals.

Invoice numbers are `INV-<year>-<sequence>`, the sequence zero-padded to 5 digits and
restarting at 1 each year. Payment is due `net_days` calendar days after the issue date;
a due date that falls on a Saturday or Sunday moves to the following Monday. Totals are
kept per currency (an invoice can bill in more than one), summing exact Decimal amounts;
a credit note is an invoice whose total is negative. Payment terms can't be negative:
`net_days` below zero is an error.

Payments are recorded against an invoice in its one currency (an invoice in several
currencies can't take payments). Its balance is the total less the payments. Its status on a
given day: "paid" when the balance is zero or less; otherwise "overdue" after the due date;
otherwise "partial" if anything has been paid, else "open".
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class InvoiceLine:
    description: str
    amount: Decimal
    currency: str = "USD"


@dataclass
class Invoice:
    number: str
    issued: dt.date
    net_days: int = 30
    lines: list[InvoiceLine] = field(default_factory=list)
    payments: list[tuple[dt.date, Decimal]] = field(default_factory=list)

    @property
    def due(self) -> dt.date:
        return due_date(self.issued, self.net_days)

    def totals(self) -> dict[str, Decimal]:
        return totals_by_currency(self.lines)

    @property
    def is_credit_note(self) -> bool:
        return any(total < 0 for total in self.totals().values())

    def _currency_total(self) -> Decimal:
        totals = self.totals()
        if len(totals) != 1:
            raise ValueError("payments need an invoice in exactly one currency")
        return next(iter(totals.values()))

    def pay(self, amount: Decimal, on: dt.date) -> None:
        if amount <= 0:
            raise ValueError("a payment must be positive")
        self._currency_total()
        self.payments.append((on, amount))

    def balance(self) -> Decimal:
        return self._currency_total() - sum((a for _, a in self.payments), Decimal("0.00"))

    def status(self, today: dt.date) -> str:
        if self.balance() <= 0:
            return "paid"
        if today > self.due:
            return "overdue"
        return "partial" if self.payments else "open"


class Numbering:
    """Hands out invoice numbers; the sequence restarts at 1 in a new year."""

    def __init__(self) -> None:
        self._last: dict[int, int] = {}

    def next(self, issued: dt.date) -> str:
        seq = self._last.get(issued.year, 0) + 1
        self._last[issued.year] = seq
        return f"INV-{issued.year}-{seq:05d}"


def due_date(issued: dt.date, net_days: int) -> dt.date:
    """`net_days` after `issued`, moved to the following Monday if it lands on a weekend."""
    if net_days < 0:
        raise ValueError("net_days must not be negative")
    due = issued + dt.timedelta(days=net_days)
    if due.weekday() >= 5:  # Saturday = 5, Sunday = 6
        due += dt.timedelta(days=7 - due.weekday())
    return due


def totals_by_currency(lines: list[InvoiceLine]) -> dict[str, Decimal]:
    """Exact totals per currency (no float rounding errors)."""
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for line in lines:
        totals[line.currency.upper()] += line.amount
    return dict(totals)
