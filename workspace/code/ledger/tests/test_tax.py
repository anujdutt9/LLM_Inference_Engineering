from decimal import Decimal

import pytest

from ledger.tax import Customer, Line, invoice_tax, is_exempt, line_tax, reverse_charge

CA = Customer("US-CA")
DE_CONSUMER = Customer("DE")
DE_BUSINESS = Customer("DE", vat_id="DE123456789")
IE_BUSINESS = Customer("IE", vat_id="IE9876543X")


def test_simple_line_tax():
    assert line_tax(Line("API", "usage", Decimal("100.00")), CA) == Decimal("7.25")


def test_tax_is_on_the_discounted_amount():
    line = Line("API", "usage", Decimal("100.00"), discount=Decimal("20.00"))
    assert line_tax(line, DE_CONSUMER) == Decimal("15.20")


def test_line_tax_rounds_half_up():
    # 2.00 x 7.25% = 0.145 -> 0.15 (half-up), not 0.14 (half-to-even)
    assert line_tax(Line("API", "usage", Decimal("2.00")), CA) == Decimal("0.15")


def test_line_tax_rounds_half_up_again():
    # 6.00 x 7.25% = 0.435 -> 0.44
    assert line_tax(Line("API", "usage", Decimal("6.00")), CA) == Decimal("0.44")


def test_exempt_categories_pay_no_tax_whatever_their_case():
    assert is_exempt(Line("Workshop", "Training", Decimal("50")))
    assert line_tax(Line("Workshop", " TRAINING ", Decimal("50")), DE_CONSUMER) == Decimal("0.00")


def test_reverse_charge_for_an_eu_business_abroad():
    assert reverse_charge(DE_BUSINESS)
    assert line_tax(Line("API", "usage", Decimal("100.00")), DE_BUSINESS) == Decimal("0.00")


def test_no_reverse_charge_in_the_sellers_own_country():
    assert not reverse_charge(IE_BUSINESS)
    assert line_tax(Line("API", "usage", Decimal("100.00")), IE_BUSINESS) == Decimal("23.00")


def test_no_reverse_charge_for_consumers():
    assert not reverse_charge(DE_CONSUMER)


def test_zero_rate_region():
    assert line_tax(Line("API", "usage", Decimal("80.00")), Customer("US-OR")) == Decimal("0.00")


def test_unknown_region_is_an_error():
    with pytest.raises(KeyError):
        line_tax(Line("API", "usage", Decimal("1")), Customer("XX"))


def test_invoice_tax_is_the_sum_of_rounded_lines():
    lines = [Line("a", "usage", Decimal("2.00")), Line("b", "usage", Decimal("2.00")), Line("c", "usage", Decimal("6.00"))]
    # 0.15 + 0.15 + 0.44, not round(10.00 x 7.25%) = 0.73
    assert invoice_tax(lines, CA) == Decimal("0.74")


def test_invoice_tax_with_discounts_and_an_exempt_line():
    lines = [
        Line("API", "usage", Decimal("200.00"), discount=Decimal("50.00")),
        Line("Workshop", "training", Decimal("300.00")),
    ]
    assert invoice_tax(lines, Customer("FR")) == Decimal("30.00")


# ---- VAT breakdown
from ledger.tax import vat_breakdown  # noqa: E402


def test_vat_breakdown_one_rate():
    lines = [Line("a", "usage", Decimal("100.00")), Line("b", "usage", Decimal("50.00"), discount=Decimal("10.00"))]
    assert vat_breakdown(lines, DE_CONSUMER) == [(Decimal("0.19"), Decimal("140.00"), Decimal("26.60"))]


def test_vat_breakdown_with_an_exempt_line():
    lines = [Line("API", "usage", Decimal("100.00")), Line("Workshop", "training", Decimal("300.00"))]
    assert vat_breakdown(lines, Customer("FR")) == [
        (Decimal("0.20"), Decimal("100.00"), Decimal("20.00")),
        (Decimal("0"), Decimal("300.00"), Decimal("0.00")),
    ]
