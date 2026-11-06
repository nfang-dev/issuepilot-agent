def price_with_tax(amount: int, percent: int) -> int:
    """Compute final whole-dollar price for an integer percentage."""
    return amount + (amount * percent // 100)
