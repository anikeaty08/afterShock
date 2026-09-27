# Aftershock — Replay Lab: metrics (§7.5 step 5). Pure functions, no I/O.

from __future__ import annotations

from dataclasses import dataclass, field

# Cumulative tier sets reported side by side: T1 alone is the
# citation-log baseline every other number is measured against.
TIER_LADDER: list[tuple[str, frozenset[str]]] = [
    ("T1", frozenset({"T1"})),
    ("T1+T2", frozenset({"T1", "T2"})),
    ("T1+T2+T3", frozenset({"T1", "T2", "T3"})),
    ("all", frozenset({"T1", "T2", "T3", "T4"})),
]

CHANGED_VERDICTS = frozenset({"CHANGED", "NOW_ABSTAINS", "NOW_ANSWERS"})


@dataclass
class PRResult:
    """One replayed PR: what Aftershock flagged (answer id -> tier) vs the
    oracle's truly-changed set (every question re-answered, §7.5 step 2)."""

    pr: int
    is_control: bool
    flagged: dict[str, str]
    truly_changed: set[str]
    total_questions: int
    reanswered: int
    impact_ms: int | None = None
    report_ms: int | None = None
    llm_calls: int = 0
    notes: list[str] = field(default_factory=list)


def recall(flagged: set[str], truly_changed: set[str]) -> float | None:
    """None when there was nothing to catch — 0/0 is not 0% recall."""
    if not truly_changed:
        return None
    return len(flagged & truly_changed) / len(truly_changed)


def precision(flagged: set[str], truly_changed: set[str]) -> float | None:
    if not flagged:
        return None
    return len(flagged & truly_changed) / len(flagged)


def flagged_at(result: PRResult, tiers: frozenset[str]) -> set[str]:
    return {aid for aid, tier in result.flagged.items() if tier in tiers}


def recall_by_tier(result: PRResult) -> dict[str, float | None]:
    return {name: recall(flagged_at(result, tiers), result.truly_changed) for name, tiers in TIER_LADDER}


def cost_saved(result: PRResult) -> float | None:
    """1 - re-answered / all questions (§7.5 step 5)."""
    if result.total_questions == 0:
        return None
    return 1.0 - result.reanswered / result.total_questions


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(int(round(p * (len(ordered) - 1))), len(ordered) - 1)
    return ordered[idx]


def _micro_recall(results: list[PRResult], tiers: frozenset[str]) -> float | None:
    """Pooled over all PRs (micro-average) — a PR with 1 changed answer
    shouldn't weigh as much as one with 30."""
    hit = total = 0
    for r in results:
        hit += len(flagged_at(r, tiers) & r.truly_changed)
        total += len(r.truly_changed)
    return hit / total if total else None


def summarize(results: list[PRResult], noise_floor: float | None = None, judge_agreement: float | None = None) -> dict:
    content = [r for r in results if not r.is_control]
    controls = [r for r in results if r.is_control]
    all_flagged = [set(r.flagged) for r in content]
    all_changed = [r.truly_changed for r in content]
    pooled_flagged = sum(len(f) for f in all_flagged)
    pooled_hits = sum(len(f & c) for f, c in zip(all_flagged, all_changed))
    impact = [float(r.impact_ms) for r in content if r.impact_ms is not None]
    report = [float(r.report_ms) for r in content if r.report_ms is not None]
    savings = [s for s in (cost_saved(r) for r in content) if s is not None]

    return {
        "prs": len(content),
        "controls": len(controls),
        "recall": {name: _micro_recall(content, tiers) for name, tiers in TIER_LADDER},
        "precision": pooled_hits / pooled_flagged if pooled_flagged else None,
        "cost_saved_mean": sum(savings) / len(savings) if savings else None,
        "control_false_positives": sum(len(r.flagged) for r in controls),
        "impact_ms": {"p50": percentile(impact, 0.5), "p95": percentile(impact, 0.95)},
        "report_ms": {"p50": percentile(report, 0.5), "p95": percentile(report, 0.95)},
        "noise_floor": noise_floor,
        "judge_agreement": judge_agreement,
        "per_pr": [
            {
                "pr": r.pr,
                "control": r.is_control,
                "flagged": len(r.flagged),
                "truly_changed": len(r.truly_changed),
                "recall_by_tier": recall_by_tier(r),
                "precision": precision(set(r.flagged), r.truly_changed),
                "cost_saved": cost_saved(r),
                "impact_ms": r.impact_ms,
                "notes": r.notes,
            }
            for r in results
        ],
    }


def cohens_kappa(human: list[str], judge: list[str]) -> float | None:
    """Judge-vs-human agreement beyond chance (§7.5 step 4)."""
    if len(human) != len(judge) or not human:
        return None
    n = len(human)
    observed = sum(h == j for h, j in zip(human, judge)) / n
    labels = set(human) | set(judge)
    expected = sum((human.count(lab) / n) * (judge.count(lab) / n) for lab in labels)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)
