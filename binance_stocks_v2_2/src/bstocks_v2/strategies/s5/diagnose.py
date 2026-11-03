"""Arm-level counts. This module does not score a mixed A/B/C win rate."""

from collections import Counter

from bstocks_v2.strategies.s5.signals import ResearchRecord
from bstocks_v2.strategies.s5.spec import ARMS, LEVELS


def summarize(records: tuple[ResearchRecord, ...] | list[ResearchRecord]) -> dict[str, object]:
    arms: dict[str, dict[str, object]] = {}
    for arm in ARMS:
        chosen = [record for record in records if record.arm == arm]
        screens = {level: 0 for level in LEVELS}
        admitted = {level: 0 for level in LEVELS}
        reasons: Counter[str] = Counter()
        labels: Counter[str] = Counter()
        splits: Counter[str] = Counter()
        for record in chosen:
            for level, passed in record.screens:
                if passed:
                    screens[level] += 1
            if record.admitted_level:
                admitted[record.admitted_level] += 1
            reasons.update(record.rejection_reasons)
            labels[record.path_label] += 1
            splits[record.split] += 1
        arms[arm] = {
            "decisions": len(chosen),
            "screens": screens,
            "admitted": {key: int(value) for key, value in admitted.items()},
            "rejection_reasons": dict(reasons),
            "path_labels": dict(labels),
            "splits": dict(splits),
            "routable": sum(1 for record in chosen if record.routable),
        }
    return {"arms": arms, "decisions": len(records), "routable": 0}


def screen_row(record: ResearchRecord) -> dict[str, str | int | bool]:
    return {
        "symbol": record.symbol,
        "arm": record.arm,
        "decision_ts": record.decision_ts.isoformat(),
        "features_hash": record.features_hash,
        "split": record.split,
        "admitted_level": record.admitted_level,
        "path_label": record.path_label,
        "news_state": record.news_state,
        "model_kind": record.model_kind,
        "simple_return": "" if record.simple_return is None else format(record.simple_return, "f"),
        "residual_z": "" if record.residual_z is None else format(record.residual_z, "f"),
        "b0": _passed(record, "B0"),
        "b1": _passed(record, "B1"),
        "b2": _passed(record, "B2"),
        "b3": _passed(record, "B3"),
    }


def _passed(record: ResearchRecord, level: str) -> bool:
    return any(name == level and flag for name, flag in record.screens)
