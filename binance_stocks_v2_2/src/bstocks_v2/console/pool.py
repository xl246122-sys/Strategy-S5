"""Candidate pool from the 479-name research file. It does not authorize live orders."""

import csv
import os
from pathlib import Path

from bstocks_v2.strategies.s1.spec import PARENTS

POOL_PATH = Path(
    os.environ.get(
        "BSTOCKS_POOL_PATH",
        r"D:\GrokBot_美股ETF股票池479_五周期_全新采集_20261101.csv",
    )
)

# These eleven sector ETFs lead every strategy board, in this order.
SECTOR_ETFS = ("XLK", "XLI", "XLV", "XLF", "XLY", "XLP", "XLE", "XLU", "XLB", "XLRE", "XLC")
SECTOR_LABEL = {
    "XLK": "信息技术",
    "XLI": "工业",
    "XLV": "医疗保健",
    "XLF": "金融",
    "XLY": "可选消费",
    "XLP": "必需消费",
    "XLE": "能源",
    "XLU": "公用事业",
    "XLB": "原材料",
    "XLRE": "房地产",
    "XLC": "通信服务",
}
_SECTOR_FILE_LABEL = {
    "XLK": "科技ETF",
    "XLI": "工业ETF",
    "XLV": "医疗ETF",
    "XLF": "金融ETF",
    "XLY": "消费ETF",
    "XLP": "必需消费",
    "XLE": "能源ETF",
    "XLU": "公用事业ETF",
    "XLB": "材料ETF",
    "XLRE": "房地产ETF",
    "XLC": "通信ETF",
}

# Industry label in the file to the sector ETF used as the residual parent.
_PARENT = {
    "科技": "XLK",
    "科技ETF": "XLK",
    "通信服务": "XLC",
    "通信ETF": "XLC",
    "可选消费": "XLY",
    "消费ETF": "XLY",
    "必需消费": "XLP",
    "金融": "XLF",
    "金融ETF": "XLF",
    "医疗保健": "XLV",
    "医疗ETF": "XLV",
    "工业": "XLI",
    "工业ETF": "XLI",
    "能源": "XLE",
    "能源ETF": "XLE",
    "原材料": "XLB",
    "材料ETF": "XLB",
    "公用事业": "XLU",
    "公用事业ETF": "XLU",
    "房地产": "XLRE",
    "房地产ETF": "XLRE",
}


def load(
    path: Path = POOL_PATH,
) -> tuple[tuple[str, ...], dict[str, str], dict[str, str | None], dict[str, str]]:
    symbols: list[str] = []
    industry: dict[str, str] = {}
    parent: dict[str, str | None] = {}
    kind: dict[str, str] = {}
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            symbol = str(row.get("当前代码", "")).strip().upper()
            if symbol == "" or symbol in seen:
                continue
            seen.add(symbol)
            label = str(row.get("行业/ETF类别", "")).strip()
            symbols.append(symbol)
            industry[symbol] = label
            parent[symbol] = _PARENT.get(label)
            kind[symbol] = str(row.get("资产类型", "")).strip()
    for symbol in SECTOR_ETFS:
        if symbol in seen:
            continue
        label = _SECTOR_FILE_LABEL[symbol]
        symbols.append(symbol)
        industry[symbol] = label
        parent[symbol] = _PARENT.get(label, symbol)
        kind[symbol] = "ETF"
        seen.add(symbol)
    return tuple(symbols), industry, parent, kind


SYMBOLS, INDUSTRY, PARENT, KIND = load()
BENCHMARKS = tuple(
    sorted(set(PARENTS) | {item for item in PARENT.values() if item} | {"SPY", "QQQ"})
)


def parent_of(symbol: str) -> str | None:
    return PARENT.get(symbol)


def industry_of(symbol: str) -> str:
    return SECTOR_LABEL.get(symbol, INDUSTRY.get(symbol, ""))


def kind_of(symbol: str) -> str:
    return KIND.get(symbol, "")
