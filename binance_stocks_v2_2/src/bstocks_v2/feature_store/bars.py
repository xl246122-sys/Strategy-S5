"""Closed-bar input and the immutable per-symbol feature snapshot."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from bstocks_v2.feature_store.hashing import sha256_rows

SCHEMA = "5"


@dataclass(frozen=True)
class FeatureBar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    bar_vwap: Decimal | None
    closed: bool = True
    feed: str = "sip"
    price_factor: Decimal = Decimal(1)


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    data_asof_utc: datetime
    bar_start: datetime
    ny_date: str
    bucket: str
    vwap_intact: bool
    close: Decimal
    vwap: Decimal | None
    rsi6: Decimal | None
    atr14: Decimal | None
    rvol_mean: Decimal | None
    rvol_median: Decimal | None
    ret: Decimal | None
    sigma_tod: Decimal | None
    ret_norm: Decimal | None
    vwap_slope: Decimal | None
    trend_z: Decimal | None
    rms6: Decimal | None
    rel_vol: Decimal | None
    baseline_days: int
    above_vwap: bool | None
    warmed: bool
    features_hash: str


def make_symbol_snapshot(
    *,
    symbol: str,
    data_asof_utc: datetime,
    bar_start: datetime,
    ny_date: str,
    bucket: str,
    vwap_intact: bool,
    close: Decimal,
    vwap: Decimal | None,
    rsi6: Decimal | None,
    atr14: Decimal | None,
    rvol_mean: Decimal | None,
    rvol_median: Decimal | None,
    ret: Decimal | None,
    sigma_tod: Decimal | None,
    ret_norm: Decimal | None,
    vwap_slope: Decimal | None,
    trend_z: Decimal | None,
    rms6: Decimal | None,
    rel_vol: Decimal | None,
    baseline_days: int,
    above_vwap: bool | None,
    warmed: bool,
) -> SymbolSnapshot:
    digest = sha256_rows(
        (
            ("schema", SCHEMA),
            ("symbol", symbol),
            ("asof", data_asof_utc.isoformat()),
            ("bucket", bucket),
            ("vwap_intact", _flag(vwap_intact)),
            ("close", _num(close)),
            ("vwap", _num(vwap)),
            ("rsi6", _num(rsi6)),
            ("atr14", _num(atr14)),
            ("rvol_mean", _num(rvol_mean)),
            ("rvol_median", _num(rvol_median)),
            ("ret", _num(ret)),
            ("sigma_tod", _num(sigma_tod)),
            ("ret_norm", _num(ret_norm)),
            ("vwap_slope", _num(vwap_slope)),
            ("trend_z", _num(trend_z)),
            ("rms6", _num(rms6)),
            ("rel_vol", _num(rel_vol)),
            ("baseline_days", str(baseline_days)),
            ("above_vwap", "" if above_vwap is None else _flag(above_vwap)),
            ("warmed", _flag(warmed)),
        )
    )
    return SymbolSnapshot(
        symbol=symbol,
        data_asof_utc=data_asof_utc,
        bar_start=bar_start,
        ny_date=ny_date,
        bucket=bucket,
        vwap_intact=vwap_intact,
        close=close,
        vwap=vwap,
        rsi6=rsi6,
        atr14=atr14,
        rvol_mean=rvol_mean,
        rvol_median=rvol_median,
        ret=ret,
        sigma_tod=sigma_tod,
        ret_norm=ret_norm,
        vwap_slope=vwap_slope,
        trend_z=trend_z,
        rms6=rms6,
        rel_vol=rel_vol,
        baseline_days=baseline_days,
        above_vwap=above_vwap,
        warmed=warmed,
        features_hash=digest,
    )


def _num(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _flag(value: bool) -> str:
    return "true" if value else "false"
