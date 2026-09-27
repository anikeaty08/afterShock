# Aftershock — Core: Fact diff across two graphs
#
# §7.3 step 4 / §8 Q1-Q2. Runs on ``docs_main`` (old side) and the
# ``docs_pr_N`` scratch copy after ``apply_changes()`` (new side, §7.3 step
# 3), scoped to the entities mentioned in the PR's changed documents on
# *either* side — not the whole graph, so this stays cheap regardless of
# corpus size.
#
# Fact key convention (§6.2): ``"{src_entity_id}|{rel_type}|{tgt_entity_id}"``
# — stable across GRAPH.COPY because entity ids are the deterministic
# ``name__type`` scheme (graphrag_sdk.ingestion.extraction_strategies
# .entity_extractors.compute_entity_id), not a graph-internal node id.

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.config import Settings, get_settings
from core.graph import GraphHandle
from core.textutil import cosine_similarity, normalize_whitespace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactRow:
    src: str
    rel: str
    tgt: str
    fact: str | None
    embedding: list[float] | None = None
    """RELATES.embedding, straight from Q2 — graphrag_sdk's finalize() embeds
    every relationship in the same space as Question.embedding, so this needs
    no extra LLM call to use for similarity (see q2_scope_facts.cypher)."""

    @property
    def key(self) -> str:
        return f"{self.src}|{self.rel}|{self.tgt}"


@dataclass
class FactDiff:
    added: dict[str, FactRow] = field(default_factory=dict)
    removed: dict[str, FactRow] = field(default_factory=dict)
    modified: dict[str, tuple[FactRow, FactRow]] = field(default_factory=dict)
    """fact_key -> (old, new) — same key, fact text differs beyond the
    normalisation + embedding-similarity threshold (re-extraction noise
    filter, §7.3 step 4)."""
    changed_entity_ids: set[str] = field(default_factory=set)
    """Endpoints of every added/removed/modified fact, plus entities present
    in the old scope but absent from the new one (disappeared entities) —
    the seed set T3's traversal (Q5) starts from."""
    disappeared_entity_ids: set[str] = field(default_factory=set)

    @property
    def changed_fact_keys(self) -> list[str]:
        """For Q4 (T2): every fact_key this diff touched, added or removed."""
        return [*self.added.keys(), *self.removed.keys(), *self.modified.keys()]


async def scope_entities(graph: GraphHandle, doc_ids: list[str]) -> set[str]:
    """Q1 — entities mentioned in the given documents on this graph."""
    if not doc_ids:
        return set()
    rows = await graph.rows_named("q1_scope_entities", {"doc_ids": doc_ids})
    return {r[0] for r in rows}


async def scope_facts(graph: GraphHandle, scope: set[str]) -> dict[str, FactRow]:
    """Q2 — every RELATES edge touching the scope, keyed by fact_key."""
    if not scope:
        return {}
    rows = await graph.rows_named("q2_scope_facts", {"scope": list(scope)})
    out: dict[str, FactRow] = {}
    for src, rel, tgt, fact, embedding in rows:
        row = FactRow(src=src, rel=rel, tgt=tgt, fact=fact, embedding=embedding)
        out[row.key] = row
    return out


def _facts_effectively_equal(
    old: FactRow, new: FactRow, theta: float, fallback_embed: dict[str, list[float]] | None = None
) -> bool:
    """Same fact_key still counts as unchanged when the text only differs by
    whitespace/casing, or — by cosine similarity at or above ``theta`` — when
    the two sides' *stored* RELATES.embedding (already in Question.embedding's
    space, no LLM call needed) say so. ``fallback_embed`` supplies an
    embedding for a side whose row has none (e.g. pre-embedding-field graph),
    keyed ``f"{key}:old"`` / ``f"{key}:new"``; still falls through to a
    conservative MODIFIED verdict when neither source has an embedding —
    recall over precision, per §4.2's "false positives cost money, not
    credibility"."""
    old_text = normalize_whitespace((old.fact or "").lower())
    new_text = normalize_whitespace((new.fact or "").lower())
    if old_text == new_text:
        return True
    fallback_embed = fallback_embed or {}
    embed_old = old.embedding or fallback_embed.get(f"{old.key}:old")
    embed_new = new.embedding or fallback_embed.get(f"{new.key}:new")
    if embed_old is not None and embed_new is not None:
        return cosine_similarity(embed_old, embed_new) >= theta
    return False


async def diff_facts(
    old_graph: GraphHandle,
    new_graph: GraphHandle,
    doc_ids: list[str],
    *,
    embedder: object | None = None,
    settings: Settings | None = None,
) -> FactDiff:
    """Compute the fact diff between the pre-PR and post-PR graphs.

    ``doc_ids`` is the PR's full changed-file list (added + modified +
    deleted paths) — Q1 simply finds nothing on the side where a given path
    doesn't exist (added docs have no old-side entities; deleted docs have
    no new-side entities), which is the correct behaviour, not a special case.

    ``embedder`` is anything exposing an async ``aembed_documents(list[str])
    -> list[list[float]]`` (graphrag_sdk's ``Embedder`` protocol —
    ``LiteLLMEmbedder`` satisfies it). Optional: pass ``None`` to skip the
    embedding-similarity re-extraction-noise filter (see
    ``_facts_effectively_equal``) — used by tests and any offline run with no
    LLM key configured.
    """
    settings = settings or get_settings()

    scope_old = await scope_entities(old_graph, doc_ids)
    scope_new = await scope_entities(new_graph, doc_ids)
    scope = scope_old | scope_new

    facts_old = await scope_facts(old_graph, scope)
    facts_new = await scope_facts(new_graph, scope)

    diff = FactDiff()
    all_keys = set(facts_old) | set(facts_new)

    # Only fall back to a live embedder call for a shared key whose stored
    # RELATES.embedding is missing on one side (old graphs ingested before
    # relationship embedding existed) — the common case needs zero LLM calls.
    shared_keys = [k for k in all_keys if k in facts_old and k in facts_new]
    fallback_embed: dict[str, list[float]] = {}
    if embedder is not None and shared_keys:
        texts: list[str] = []
        text_owners: list[tuple[str, str]] = []  # (key, "old"|"new")
        for k in shared_keys:
            old_row, new_row = facts_old[k], facts_new[k]
            if old_row.fact and new_row.fact and old_row.fact != new_row.fact:
                if old_row.embedding is None:
                    texts.append(old_row.fact)
                    text_owners.append((k, "old"))
                if new_row.embedding is None:
                    texts.append(new_row.fact)
                    text_owners.append((k, "new"))
        if texts:
            try:
                vectors = await embedder.aembed_documents(texts)  # type: ignore[attr-defined]
                for (key, side), vec in zip(text_owners, vectors):
                    fallback_embed[f"{key}:{side}"] = vec
            except Exception:
                logger.warning("Fact-text embedding failed; falling back to exact-match diff", exc_info=True)

    for key in all_keys:
        old_row = facts_old.get(key)
        new_row = facts_new.get(key)
        if old_row is not None and new_row is None:
            diff.removed[key] = old_row
            diff.changed_entity_ids.update((old_row.src, old_row.tgt))
        elif old_row is None and new_row is not None:
            diff.added[key] = new_row
            diff.changed_entity_ids.update((new_row.src, new_row.tgt))
        else:
            assert old_row is not None and new_row is not None
            equal = _facts_effectively_equal(
                old_row,
                new_row,
                settings.fact_text_similarity_theta,
                fallback_embed,
            )
            if not equal:
                diff.modified[key] = (old_row, new_row)
                diff.changed_entity_ids.update((new_row.src, new_row.tgt))

    diff.disappeared_entity_ids = scope_old - scope_new
    diff.changed_entity_ids.update(diff.disappeared_entity_ids)

    logger.info(
        "Fact diff: %d added, %d removed, %d modified, %d changed entities (%d disappeared)",
        len(diff.added),
        len(diff.removed),
        len(diff.modified),
        len(diff.changed_entity_ids),
        len(diff.disappeared_entity_ids),
    )
    return diff
