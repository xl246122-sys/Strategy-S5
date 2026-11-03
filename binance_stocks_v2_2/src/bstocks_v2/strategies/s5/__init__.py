"""S5 end-of-day research. Live trading is locked off. The mock book is not the global guardian.

Heavy research modules load on first attribute access so a paper desk can import
``spec`` and ``lock`` without the session-calendar research stack.
"""

__all__ = [
    "ResearchRecord",
    "S5GuardianPolicy",
    "S5ResearchBook",
    "evaluate",
    "research_policy",
    "s5_live_enabled",
]

_LAZY = {
    "S5ResearchBook": ("bstocks_v2.strategies.s5.book", "S5ResearchBook"),
    "s5_live_enabled": ("bstocks_v2.strategies.s5.lock", "s5_live_enabled"),
    "S5GuardianPolicy": ("bstocks_v2.strategies.s5.policy", "S5GuardianPolicy"),
    "research_policy": ("bstocks_v2.strategies.s5.policy", "research_policy"),
    "ResearchRecord": ("bstocks_v2.strategies.s5.signals", "ResearchRecord"),
    "evaluate": ("bstocks_v2.strategies.s5.signals", "evaluate"),
}


def __getattr__(name: str):
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    import importlib

    value = getattr(importlib.import_module(module_name), attr)
    globals()[name] = value
    return value
