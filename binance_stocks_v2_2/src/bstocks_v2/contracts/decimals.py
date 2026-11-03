"""Decimal parsing. Float is rejected so money never enters binary approximation."""

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator


def parse_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("float is forbidden for prices, amounts, and quantities")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError(f"cannot parse decimal from {type(value).__name__}")


def require_price_scale(value: Decimal) -> Decimal:
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        raise ValueError("price must have at most 2 decimal places")
    return value


Quantity = Annotated[Decimal, BeforeValidator(parse_decimal)]
Money = Annotated[Decimal, BeforeValidator(parse_decimal)]
Price = Annotated[Decimal, BeforeValidator(parse_decimal), AfterValidator(require_price_scale)]
