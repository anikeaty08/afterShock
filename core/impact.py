# Aftershock — Core: Impact tiers
#
# §4.2 / §7.3 steps 5-6 / §8 Q3-Q6, Q11. Runs entirely on ``docs_main``
# *before* the PR's changes are applied there (§6.3 design note: the ledger's
# USED_CHUNK/USED_ENTITY edges must still be intact), against the FactDiff
# computed in diff.py.
#
# T3's semantic gate and T4's reverse retrieval both compare stored
# embeddings already in the graph (Question.embedding, RELATES.embedding —
# see q2_scope_facts.cypher's comment) rather than re-embedding anything, so
# this module makes zero LLM calls of its own.

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.config import Settings, get_settings
from core.diff import FactDiff, FactRow
from core.graph import GraphHandle
from core.textutil import cosine_similarity

logger = logging.getLogger(__name__)

# §7.3 step 6: "sort T1 > T2 > T4 > T3 by score, cap at MAX_RECHECKS".
TIER_PRIORITY = {"T1": 0, "T2": 1, "T4": 2, "T3": 3}


@dataclass
class ImpactCandidate:
    answer_id: str
    tier: str
    via: list[str] = field(default_factory=list)
    score: float | None = None
    """Only meaningful for T4 (cosine similarity to the triggering fact)."""


@dataclass
class ImpactResult:
    candidates: list[ImpactCandidate]
    """Deduplicated (one entry per answer_id, at its highest-priority tier),
    sorted, and capped at ``settings.max_rechecks_per_pr``."""
    tier_raw_counts: dict[str, int]
    """Pre-dedup, pre-cap counts per tier — what the Replay Lab's
    recall-by-tier metric (§7.5) needs: T1-only vs T1+T2 vs T1+T2+T3 vs all."""
    truncated: bool
    """True if the budget cap actually dropped candidates (§7.3 step 6: the
    PR comment must say so when it does)."""


async def compute_hub_ids(graph: GraphHandle, percentile: float) -> set[str]:
    """Q11 + Q11b — entities above the given RELATES-degree percentile.

    Computed fresh per PR run rather than cached: cheap (one scan of
    ``__Entity__``-`RELATES` degree) and always current, including any
    merges since the last computation (§7.4).
    """
    rows = await graph.rows_named("q11b_degree_distribution", {})
    if not rows:
        return set()
    degrees = sorted(r[1] for r in rows)
    idx = min(int(len(degrees) * percentile), len(degrees) - 1)
    threshold = degrees[idx]
    hub_rows = await graph.rows_named("q11_hub_entities", {"hub_threshold": threshold})
    return {r[0] for r in hub_rows}


async def t1_cited(graph: GraphHandle, doc_ids: list[str]) -> list[ImpactCandidate]:
    """Q3 — answers that used a chunk of a changed document."""
    if not doc_ids:
        return []
    rows = await graph.rows_named("q3_t1_cited", {"doc_ids": doc_ids})
    return [ImpactCandidate(answer_id=r[0], tier="T1", via=list(r[1])) for r in rows]


async def t2_fact(graph: GraphHandle, diff: FactDiff) -> list[ImpactCandidate]:
    """Q4 — answers whose context contained a fact the PR removed or
    modified. Deliberately excludes *added* facts (§4.2's own definition):
    an existing answer's ``fact_keys`` can only ever list facts that existed
    in the graph when it was generated, so a brand-new key could never
    appear there anyway — passing it would be harmless but is left out for
    clarity."""
    changed = [*diff.removed.keys(), *diff.modified.keys()]
    if not changed:
        return []
    rows = await graph.rows_named("q4_t2_fact", {"changed_fact_keys": changed})
    return [ImpactCandidate(answer_id=r[0], tier="T2", via=list(r[1])) for r in rows]


def _changed_fact_embedding(row: FactRow) -> list[float] | None:
    return row.embedding


async def t3_neighbour(
    graph: GraphHandle,
    diff: FactDiff,
    hub_ids: set[str],
    already_flagged: set[str],
    *,
    settings: Settings | None = None,
) -> list[ImpactCandidate]:
    """Q5 + Q5b — changed entity -> (neighbour | itself) -> answer, hub-
    dampened, then semantically gated (theta3) against every changed fact's
    stored embedding (§4.2 T3, §8 Q5).

    ``already_flagged`` should be every answer_id T1/T2 already caught —
    Q5/Q5b exclude them at the Cypher level so the (comparatively expensive)
    semantic gate below only runs on genuinely new candidates.
    """
    settings = settings or get_settings()
    changed_entity_ids = list(diff.changed_entity_ids)
    if not changed_entity_ids:
        return []

    already = list(already_flagged)
    neighbour_rows = await graph.rows_named(
        "q5_t3_neighbour",
        {
            "changed_entity_ids": changed_entity_ids,
            "hub_ids": list(hub_ids),
            "already_flagged": already,
        },
    )
    direct_rows = await graph.rows_named(
        "q5b_t3_direct",
        {"changed_entity_ids": changed_entity_ids, "already_flagged": already},
    )

    merged: dict[str, list[str]] = {}
    for answer_id, via in [*((r[0], r[1]) for r in neighbour_rows), *((r[0], r[1]) for r in direct_rows)]:
        merged.setdefault(answer_id, [])
        for v in via:
            if v not in merged[answer_id]:
                merged[answer_id].append(v)
    if not merged:
        return []

    # Semantic gate: every changed fact's stored embedding (added, removed,
    # modified — new side preferred, old side for a pure removal) forms the
    # comparison set; a candidate survives if its question is within theta3
    # of at least one of them.
    fact_embeddings: list[list[float]] = []
    for row in [*diff.added.values(), *diff.removed.values(), *(new for _, new in diff.modified.values())]:
        emb = _changed_fact_embedding(row)
        if emb is not None:
            fact_embeddings.append(emb)

    candidates: list[ImpactCandidate] = []
    if not fact_embeddings:
        # No stored embeddings to gate against (e.g. pre-embedding-field
        # graph) — degrade to "hub-dampening only", matching diff.py's own
        # graceful fallback rather than silently dropping every T3 candidate.
        logger.info("T3 semantic gate skipped: no changed-fact embeddings available")
        return [ImpactCandidate(answer_id=aid, tier="T3", via=via) for aid, via in merged.items()]

    for answer_id, via in merged.items():
        q_rows = await graph.rows_named(
            "q19_question_embedding_for_answer", {"answer_id": answer_id}
        )
        if not q_rows or q_rows[0][0] is None:
            continue
        q_embedding = q_rows[0][0]
        best = max(cosine_similarity(q_embedding, fe) for fe in fact_embeddings)
        if best >= settings.t3_semantic_gate_theta:
            candidates.append(ImpactCandidate(answer_id=answer_id, tier="T3", via=via, score=best))
    return candidates


async def t4_reverse_retrieval(
    graph: GraphHandle,
    diff: FactDiff,
    already_flagged: set[str],
    *,
    k: int = 10,
    settings: Settings | None = None,
) -> list[ImpactCandidate]:
    """Q6 — every added/modified fact's stored embedding, searched against
    the Question vector index (§4.2 T4). Removed facts are excluded on
    purpose: there is no new fact text to search *from* once it's gone, and
    a question close to a deleted fact is exactly what T1 (if it cited the
    doc) or the coverage-regression check (NOW_ABSTAINS, §9) already catches.
    """
    settings = settings or get_settings()
    facts = [*diff.added.values(), *(new for _, new in diff.modified.values())]
    candidates: dict[str, ImpactCandidate] = {}

    for row in facts:
        embedding = _changed_fact_embedding(row)
        if embedding is None:
            continue
        rows = await graph.rows_named(
            "q6_t4_reverse_retrieval", {"fact_embedding": embedding, "k": k}
        )
        for answer_id, question_text, distance, _tier in rows:
            if answer_id in already_flagged:
                continue
            similarity = 1.0 - distance
            if similarity < settings.t4_similarity_theta:
                continue
            existing = candidates.get(answer_id)
            if existing is None or similarity > (existing.score or 0.0):
                candidates[answer_id] = ImpactCandidate(
                    answer_id=answer_id,
                    tier="T4",
                    via=[f"{row.src} —[{row.rel}]→ {row.tgt}"],
                    score=similarity,
                )
    return list(candidates.values())


async def compute_impact(
    graph: GraphHandle,
    diff: FactDiff,
    doc_ids: list[str],
    *,
    settings: Settings | None = None,
) -> ImpactResult:
    """Run all four tiers in priority order and merge (§7.3 steps 5-6).

    Each tier after T1 is told what's already flagged, both so Q5/Q5b's own
    ``NOT a.id IN $already_flagged`` filter can do its job and so T3/T4 don't
    spend a semantic-gate lookup on an answer that's already going to be
    reported at a higher-priority tier anyway.
    """
    settings = settings or get_settings()

    t1 = await t1_cited(graph, doc_ids)
    flagged = {c.answer_id for c in t1}

    t2 = await t2_fact(graph, diff)
    flagged |= {c.answer_id for c in t2}

    hub_ids = await compute_hub_ids(graph, settings.hub_degree_percentile)
    t3 = await t3_neighbour(graph, diff, hub_ids, flagged, settings=settings)
    flagged |= {c.answer_id for c in t3}

    t4 = await t4_reverse_retrieval(graph, diff, flagged, settings=settings)

    tier_raw_counts = {"T1": len(t1), "T2": len(t2), "T3": len(t3), "T4": len(t4)}

    # Merge, keep each answer at its single highest-priority tier, sort.
    by_answer: dict[str, ImpactCandidate] = {}
    for c in [*t1, *t2, *t4, *t3]:
        existing = by_answer.get(c.answer_id)
        if existing is None or TIER_PRIORITY[c.tier] < TIER_PRIORITY[existing.tier]:
            by_answer[c.answer_id] = c

    ordered = sorted(
        by_answer.values(),
        key=lambda c: (TIER_PRIORITY[c.tier], -(c.score or 0.0)),
    )

    truncated = len(ordered) > settings.max_rechecks_per_pr
    capped = ordered[: settings.max_rechecks_per_pr]

    logger.info(
        "Impact: T1=%d T2=%d T3=%d T4=%d -> %d unique (capped=%s, truncated=%s)",
        tier_raw_counts["T1"],
        tier_raw_counts["T2"],
        tier_raw_counts["T3"],
        tier_raw_counts["T4"],
        len(capped),
        len(capped) != len(ordered),
        truncated,
    )

    return ImpactResult(candidates=capped, tier_raw_counts=tier_raw_counts, truncated=truncated)
