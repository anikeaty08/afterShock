from eval.metrics import PRResult, cohens_kappa, cost_saved, percentile, recall, recall_by_tier, summarize


def _pr(flagged, changed, total=10, control=False, impact=None):
    return PRResult(pr=1, is_control=control, flagged=flagged, truly_changed=set(changed),
                    total_questions=total, reanswered=len(flagged), impact_ms=impact)


def test_recall_none_when_nothing_changed():
    assert recall({"a"}, set()) is None


def test_recall_by_tier_is_cumulative():
    r = _pr({"a": "T1", "b": "T2", "c": "T3", "d": "T4"}, ["a", "b", "c", "d"])
    assert recall_by_tier(r) == {"T1": 0.25, "T1+T2": 0.5, "T1+T2+T3": 0.75, "all": 1.0}


def test_cost_saved():
    assert cost_saved(_pr({"a": "T1", "b": "T1"}, [], total=10)) == 0.8


def test_summary_pools_recall_and_counts_control_false_positives():
    results = [
        _pr({"a": "T1"}, ["a", "b"]),
        _pr({"c": "T2"}, ["c"]),
        _pr({"x": "T1"}, [], control=True),
    ]
    s = summarize(results)
    assert s["recall"]["T1"] == 1 / 3
    assert s["recall"]["T1+T2"] == 2 / 3
    assert s["precision"] == 1.0
    assert s["control_false_positives"] == 1
    assert s["prs"] == 2 and s["controls"] == 1


def test_percentile():
    assert percentile([10, 20, 30, 40, 50], 0.5) == 30
    assert percentile([], 0.5) is None


def test_cohens_kappa():
    assert cohens_kappa(["A", "B", "A", "B"], ["A", "B", "A", "B"]) == 1.0
    assert cohens_kappa(["A", "A", "B", "B"], ["A", "B", "A", "B"]) == 0.0
