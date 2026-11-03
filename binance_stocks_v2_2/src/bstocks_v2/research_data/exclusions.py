"""Hard universe exclusions. Taiwan listings such as TSM stay in the pool."""

CHINA_MAINLAND_US_LISTED = frozenset(
    {
        "API",
        "ATAT",
        "ATHM",
        "BABA",
        "BEKE",
        "BGNE",
        "BIDU",
        "BILI",
        "BZ",
        "BZUN",
        "CSIQ",
        "DAQO",
        "DADA",
        "DIDI",
        "DQ",
        "EDU",
        "EH",
        "FINV",
        "FUTU",
        "GDS",
        "GOTU",
        "HCM",
        "HOLI",
        "HSAI",
        "HUYA",
        "IQ",
        "JD",
        "JFIN",
        "JKS",
        "KC",
        "LEGN",
        "LI",
        "LU",
        "LX",
        "MNSO",
        "MOMO",
        "NIO",
        "NOAH",
        "NTES",
        "PDD",
        "QFIN",
        "RLX",
        "TAL",
        "TCOM",
        "TIGR",
        "TME",
        "TUYA",
        "VIPS",
        "VNET",
        "WB",
        "XNET",
        "XPEV",
        "XYF",
        "YMM",
        "YY",
        "YUMC",
        "ZH",
        "ZKH",
        "ZLAB",
        "ZTO",
    }
)
_SPACEX = frozenset({"SPCX", "SPACEX"})


def hard_exclusion(symbol: str) -> str | None:
    """Return a reason code, or None when the symbol is allowed to be tested further."""

    upper = symbol.upper()
    if upper in _SPACEX:
        return "spacex"
    if upper in CHINA_MAINLAND_US_LISTED:
        return "china_mainland_us_listed"
    return None
