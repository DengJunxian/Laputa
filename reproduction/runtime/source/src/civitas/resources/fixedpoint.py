"""Integer products and rational fees: no binary64 monetary operations."""
from decimal import Decimal, InvalidOperation

from civitas.contracts.portfolio import MAX_INT, Rate


def checked(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_INT:
        raise ValueError('Amount must be a nonnegative signed-64-bit integer')
    return value


def notional(price_ticks: int, tick_minor: int, quantity: int) -> int:
    return checked(checked(price_ticks)*checked(tick_minor)*checked(quantity))


def fee(amount: int, rate: Rate) -> int:
    return checked(checked(amount)*rate.numerator // rate.denominator)


def decimal_minor(text: str, scale: int = 1_000_000) -> int:
    if type(text) is not str or type(scale) is not int or scale < 1 or str(scale).strip('0') != '1':
        raise ValueError('Explicit decimal text and power-of-ten scale required')
    try:
        value = Decimal(text)
        if not value.is_finite() or value < 0:
            raise ValueError('Finite nonnegative decimal required')
        numerator, denominator = value.as_integer_ratio()
        units, remainder = divmod(numerator*scale, denominator)
        if remainder:
            raise ValueError('Amount exceeds declared precision')
        return checked(units)
    except InvalidOperation as exc:
        raise ValueError('Invalid decimal amount') from exc
