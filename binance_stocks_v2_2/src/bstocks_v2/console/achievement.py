"""Live achievement for one strategy.

The percent is how close the live quote is to that strategy's geometry.
It stays in 0..99. Only a closed bar may record 100 and raise the alert.
S5 uses the same scale and still cannot place an order.
"""

from decimal import Decimal

from bstocks_v2.strategies.s5.spec import B0_MAX_SIMPLE_RETURN

_ZERO = Decimal(0)
_ONE = Decimal(1)
Part = tuple[str, Decimal]


def quote_achievement(
    strategy: str,
    prices: tuple[Decimal, ...],
    parent_prices: tuple[Decimal, ...] = (),
    *,
    has_parent: bool = False,
    session: str = "OVERNIGHT",
) -> tuple[str, Decimal, int]:
    """Return the open gate, how close that gate is, and the percent earned."""

    if strategy == "S1":
        return _score(_s1(prices, parent_prices, has_parent=has_parent))
    if strategy == "S2":
        return _score(_s2(prices))
    if strategy == "S3":
        return _score(
            _s3(prices, session),
            gate=None if session == "RTH" else "WINDOW_OPEN",
        )
    if strategy == "S4":
        return _score(_s4(prices))
    if strategy == "S5":
        return _score(_s5(prices, parent_prices, has_parent=has_parent))
    return "WARMUP", _ZERO, 0


def gate_label(reason: str) -> str:
    return _LABELS.get(reason, reason)


def _score(parts: tuple[Part, ...], *, gate: str | None = None) -> tuple[str, Decimal, int]:
    if not parts:
        return "WARMUP", _ZERO, 0
    units = tuple(_cap(value) for _, value in parts)
    progress = int(sum(units, _ZERO) / Decimal(len(units)) * Decimal(99))
    progress = min(99, max(0, progress))
    if gate is not None:
        edge = next((value for name, value in parts if name == gate), _ZERO)
        return gate, edge, progress
    index = min(range(len(parts)), key=lambda item: (units[item], item))
    name, edge = parts[index]
    return name, edge, progress


def _s1(
    prices: tuple[Decimal, ...],
    parent_prices: tuple[Decimal, ...],
    *,
    has_parent: bool,
) -> tuple[Part, ...]:
    last = prices[-1] if prices else _ZERO
    center = _mean(prices)
    span = _span(prices)
    residual = _residual(prices, parent_prices)
    mean = _mean(residual)
    sigma = _deviation(residual) if len(residual) >= 2 else _ZERO
    z_score = (residual[-1] - mean) / sigma if residual and sigma > 0 else _ZERO
    lift = (last - center) / span if span > 0 else _ZERO
    parent_move = _move(parent_prices)
    return (
        ("NO_SECTOR", _ONE if has_parent else _ZERO),
        ("WARMUP", _cap(Decimal(min(len(prices), len(parent_prices))) / Decimal(8))),
        ("BELOW_VWAP", _peak(lift, Decimal("0.35"), Decimal("0.35"))),
        ("EXTENDED", _peak(lift, Decimal("0.55"), Decimal("0.45"))),
        ("ALPHA", _peak(mean, Decimal("0.0015"), Decimal("0.0025"))),
        ("Z", _peak(z_score, Decimal("2.5"), Decimal("2.5"))),
        ("SECTOR", _peak(parent_move, Decimal("0.004"), Decimal("0.008"))),
    )


def _s2(prices: tuple[Decimal, ...]) -> tuple[Part, ...]:
    move = _move(prices)
    span = _span(prices)
    last = prices[-1] if prices else _ZERO
    low = min(prices) if prices else _ZERO
    high = max(prices) if prices else _ZERO
    location = (last - low) / span if span > 0 else _ZERO
    dip = _interior_dip(prices)
    if dip is None or high <= dip:
        depth = _ZERO
        reclaim = _ZERO
    else:
        depth = (high - dip) / span if span > 0 else _ZERO
        reclaim = (last - dip) / (high - dip)
    return (
        ("WARMUP", _cap(Decimal(len(prices)) / Decimal(8))),
        ("SLOPE", _cap(move / Decimal("0.006")) if move > 0 else _ZERO),
        ("NO_DIP", _peak(depth, Decimal("0.40"), Decimal("0.40"))),
        ("NO_CROSS", _peak(reclaim, Decimal("0.62"), Decimal("0.38"))),
        ("STRUCTURE", _peak(move, Decimal("0.008"), Decimal("0.012"))),
        ("EXTENDED", _peak(location, Decimal("0.70"), Decimal("0.30"))),
    )


def _s3(prices: tuple[Decimal, ...], session: str) -> tuple[Part, ...]:
    last = prices[-1] if prices else _ZERO
    span = _span(prices)
    low = min(prices) if prices else _ZERO
    early = prices[:3]
    early_high = max(early) if early else last
    width = span / last if last > 0 else _ZERO
    location = (last - low) / span if span > 0 else _ZERO
    overshoot = (last - early_high) / last if last > 0 else _ZERO
    above = (last - _mean(prices)) / span if span > 0 else _ZERO
    return (
        ("NO_RANGE", _cap(Decimal(len(prices)) / Decimal(6))),
        ("WINDOW_OPEN", _ONE if session == "RTH" else _ZERO),
        ("ATR", _peak(width, Decimal("0.004"), Decimal("0.006"))),
        ("INSIDE_RANGE", _peak(location, Decimal("0.84"), Decimal("0.28"))),
        ("EXTENDED", _peak(overshoot, Decimal("0.0015"), Decimal("0.004"))),
        ("BELOW_VWAP", _peak(above, Decimal("0.25"), Decimal("0.35"))),
    )


def _s4(prices: tuple[Decimal, ...]) -> tuple[Part, ...]:
    last = prices[-1] if prices else _ZERO
    center = _mean(prices)
    span = _span(prices)
    low = min(prices) if prices else _ZERO
    stretch = (center - last) / span if span > 0 else _ZERO
    bounce = (last - prices[-2]) / span if len(prices) >= 2 and span > 0 else _ZERO
    retrace = (last - low) / (center - low) if center > low else _ZERO
    return (
        ("WARMUP", _cap(Decimal(len(prices)) / Decimal(8))),
        ("ABOVE_VWAP", _peak(stretch, Decimal("0.40"), Decimal("0.40"))),
        ("NO_STRETCH", _peak(stretch, Decimal("0.55"), Decimal("0.35"))),
        ("NO_DIP", _cap(stretch / Decimal("0.20")) if stretch > 0 else _ZERO),
        ("NO_BOUNCE", _peak(bounce, Decimal("0.12"), Decimal("0.16"))),
        ("NO_CROSS", _peak(retrace, Decimal("0.35"), Decimal("0.35"))),
        ("GEOMETRY", _peak(_move(prices), Decimal("-0.006"), Decimal("0.010"))),
    )


def _s5(
    prices: tuple[Decimal, ...],
    parent_prices: tuple[Decimal, ...],
    *,
    has_parent: bool,
) -> tuple[Part, ...]:
    last = prices[-1] if prices else _ZERO
    high = max(prices) if prices else _ZERO
    low = min(prices) if prices else _ZERO
    span = _span(prices)
    fade = (high - last) / last if last > 0 else _ZERO
    simple = _move(prices)
    drop = -simple
    residual = _mean(_residual(prices, parent_prices))
    turning = len(prices) >= 2 and last >= prices[-2] and last > low
    held = (last - low) / span if span > 0 else _ZERO
    return (
        ("WARMUP", _cap(Decimal(len(prices)) / Decimal(8))),
        ("NO_SECTOR", _cap(Decimal(len(parent_prices)) / Decimal(8)) if has_parent else _ZERO),
        ("NO_STRETCH", _peak(fade, Decimal("0.008"), Decimal("0.012"))),
        ("Z", _peak(drop, -B0_MAX_SIMPLE_RETURN, Decimal("0.025"))),
        ("BELOW_VWAP", _peak(drop, Decimal("0.012"), Decimal("0.018"))),
        ("NO_BOUNCE", _peak(held, Decimal("0.25"), Decimal("0.25")) if turning else _ZERO),
        ("SECTOR", _peak(-residual, Decimal("0.001"), Decimal("0.002"))),
    )


def _interior_dip(prices: tuple[Decimal, ...]) -> Decimal | None:
    best: Decimal | None = None
    for index in range(1, len(prices) - 1):
        price = prices[index]
        if price <= prices[index - 1] and price < prices[index + 1]:
            if best is None or price < best:
                best = price
    return best


def _residual(
    prices: tuple[Decimal, ...],
    parent_prices: tuple[Decimal, ...],
) -> tuple[Decimal, ...]:
    stock = _returns(prices)
    sector = _returns(parent_prices)
    width = min(len(stock), len(sector))
    return tuple(stock[-width + index] - sector[-width + index] for index in range(width))


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return _ZERO
    return sum(values, _ZERO) / Decimal(len(values))


def _span(prices: tuple[Decimal, ...]) -> Decimal:
    if not prices:
        return _ZERO
    return max(prices) - min(prices)


def _move(prices: tuple[Decimal, ...]) -> Decimal:
    if len(prices) < 2 or prices[0] <= 0:
        return _ZERO
    return (prices[-1] - prices[0]) / prices[0]


def _returns(prices: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
    out: list[Decimal] = []
    for previous, current in zip(prices, prices[1:], strict=False):
        if previous <= 0:
            continue
        out.append((current - previous) / previous)
    return tuple(out)


def _deviation(values: tuple[Decimal, ...]) -> Decimal:
    center = _mean(values)
    variance = sum(((item - center) ** 2 for item in values), _ZERO) / Decimal(len(values))
    return variance.sqrt()


def _peak(value: Decimal, center: Decimal, width: Decimal) -> Decimal:
    """One at the center, zero once the value is a full width away."""

    if width <= 0:
        return _ZERO
    distance = abs(value - center)
    if distance >= width:
        return _ZERO
    return _ONE - distance / width


def _cap(value: Decimal) -> Decimal:
    if value <= 0:
        return _ZERO
    if value >= 1:
        return _ONE
    return value


_LABELS = {
    "NO_SECTOR": "无行业",
    "FACTOR_WARMUP": "因子未齐",
    "ATR": "波动不足",
    "BELOW_VWAP": "在均价下",
    "EXTENDED": "偏离过大",
    "WARMUP": "样本不足",
    "VARIANCE": "残差太平",
    "ALPHA": "残差未转正",
    "Z": "强度不够",
    "SECTOR": "行业未跟上",
    "GEOMETRY": "形态已齐",
    "SLOPE": "斜率未起",
    "NO_DIP": "没有回踩",
    "NO_CROSS": "没有收复",
    "STRUCTURE": "结构已齐",
    "ABOVE_VWAP": "仍在均价上",
    "NO_STRETCH": "跌幅不够",
    "NO_BOUNCE": "尚未反弹",
    "WINDOW_OPEN": "开盘窗未到",
    "NO_RANGE": "区间未成",
    "LOCK_BAR": "区间未锁",
    "INSIDE_RANGE": "仍在区间内",
    "ALREADY_BROKE": "已离开区间",
    "S5_ROUTE_CLOSED": "研究路由关闭",
    "SIGNAL": "信号达成",
    "AWAITING_VENUE": "等待交易资格",
    "ENTRY_WINDOW": "不在开仓窗",
}
