from calculator import price_with_tax


def test_zero_tax():
    assert price_with_tax(100, 0) == 100


def test_ten_percent_tax():
    assert price_with_tax(100, 10) == 110


def test_tax_calculation_should_round_nearest_cent():
    assert price_with_tax(99, 5) == 104
