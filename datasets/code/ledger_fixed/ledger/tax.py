"""Sales tax / VAT on invoice lines.

Tax is charged on what the customer pays: the line amount after its discount. Each line's
tax is rounded to the cent half-up (0.005 -> 0.01), as tax authorities expect, and the
invoice's tax is the sum of the rounded lines. Some categories are exempt everywhere
(category names are matched without regard to case). A business customer in another EU
country with a VAT id pays no VAT (reverse charge): they account for it themselves. A blank
VAT id (empty or only spaces) is no VAT id.

An invoice shows a VAT breakdown: one row per rate actually applied to its lines (exempt and
reverse-charged lines are a 0% row), each with the taxable amount (after discounts) and the
tax (the sum of that rate's rounded line taxes), highest rate first.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")

RATES = {  # region -> rate
    "US-CA": Decimal("0.0725"),
    "US-NY": Decimal("0.04"),
    "US-OR": Decimal("0"),
    "DE": Decimal("0.19"),
    "FR": Decimal("0.20"),
    "IE": Decimal("0.23"),
    "GB": Decimal("0.20"),
}
EU = {"DE", "FR", "IE"}
EXEMPT_CATEGORIES = {"training", "donation"}
SELLER_COUNTRY = "IE"


@dataclass(frozen=True)
class Line:
    description: str
    category: str
    amount: Decimal  # before discount
    discount: Decimal = Decimal("0")  # an amount, already computed by discounts.py


@dataclass(frozen=True)
class Customer:
    region: str
    vat_id: str | None = None  # a business customer's VAT id, if any


def is_exempt(line: Line) -> bool:
    return line.category.strip().lower() in EXEMPT_CATEGORIES


def reverse_charge(customer: Customer) -> bool:
    """EU business customer in another EU country: no VAT charged."""
    has_id = bool(customer.vat_id and customer.vat_id.strip())
    return has_id and customer.region in EU and customer.region != SELLER_COUNTRY


def rate_for(customer: Customer) -> Decimal:
    if customer.region not in RATES:
        raise KeyError(f"no tax rate for region {customer.region!r}")
    return RATES[customer.region]


def line_tax(line: Line, customer: Customer) -> Decimal:
    """The tax on one line, on its discounted amount, rounded half-up to the cent."""
    if is_exempt(line) or reverse_charge(customer):
        return Decimal("0.00")
    taxable = line.amount - line.discount
    return (taxable * rate_for(customer)).quantize(CENT, rounding=ROUND_HALF_UP)


def vat_breakdown(lines: list[Line], customer: Customer) -> list[tuple[Decimal, Decimal, Decimal]]:
    """(rate, taxable amount after discounts, tax) per rate applied, highest rate first; exempt and
    reverse-charged lines are rate 0."""
    rows: dict[Decimal, list[Decimal]] = {}
    for line in lines:
        rate = Decimal("0") if is_exempt(line) or reverse_charge(customer) else rate_for(customer)
        row = rows.setdefault(rate, [Decimal("0.00"), Decimal("0.00")])
        row[0] += line.amount - line.discount
        row[1] += line_tax(line, customer)
    return [(rate, taxable, tax) for rate, (taxable, tax) in sorted(rows.items(), key=lambda r: r[0], reverse=True)]


def invoice_tax(lines: list[Line], customer: Customer) -> Decimal:
    """The sum of the lines' rounded taxes."""
    return sum((line_tax(line, customer) for line in lines), Decimal("0.00"))
