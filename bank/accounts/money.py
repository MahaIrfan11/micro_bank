from decimal import Decimal, InvalidOperation

CURRENCY = "USD"
DECIMALS = 2
_SCALE = Decimal(10) ** DECIMALS


def parse_amount(raw):
    """Parse to exact Decimal. Rejects float -- send amount as a string."""
    if isinstance(raw, float):
        raise ValueError(
            'Amount must be sent as a string (e.g. "10.75"), not a JSON number.'
        )
    if isinstance(raw, Decimal):
        return raw
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        raise ValueError(f"'{raw}' is not a valid decimal amount")


def to_minor_units(amount, *, require_positive=True):
    """Decimal('10.75') -> 1075 cents. Rejects sub-cent precision."""
    amount = parse_amount(amount)

    exponent = amount.as_tuple().exponent
    if isinstance(exponent, str):  # NaN / Infinity
        raise ValueError(f"'{amount}' is not a finite amount")
    if -exponent > DECIMALS:
        raise ValueError(f"USD supports at most {DECIMALS} decimal places, got '{amount}'")
    if require_positive and amount <= 0:
        raise ValueError("Amount must be greater than zero")

    return int(amount * _SCALE)


def to_major_units(amount_minor):
    """1075 -> Decimal('10.75')"""
    return (Decimal(amount_minor) / _SCALE).quantize(Decimal("0.01"))


def format_amount(amount_minor):
    return str(to_major_units(amount_minor))
