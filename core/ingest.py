# Aftershock — Core: Ingestion, PR flow, merge flow
#
# The orchestration layer: §7.1 (bootstrap), §7.3 (PR flow), §7.4 (merge
# flow). Every step that talks to an LLM (ingestion, re-answering) is behind
# a small injectable seam — ``apply_changes_fn`` / ``answer_fn`` — so the
# graph-only wiring (GRAPH.COPY, fact diff, impact tiers, ledger writes) is
# fully testable against a live FalkorDB without an LLM key, and a caller
# with a real key gets the real thing by just not overriding the defaults.

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from graphrag_sdk import (
    ApplyChangesResult,
    BatchEntry,
    ConnectionConfig,
    GraphRAG,
    LiteLLM,
    LiteLLMEmbedder,
    Ontology,
)
from graphrag_sdk.ingestion.chunking_strategies.structural_chunking import StructuralChunking
from graphrag_sdk.ingestion.loaders.base import LoaderStrategy
from graphrag_sdk.ingestion.loaders.markdown_loader import MarkdownLoader

from core.config import Settings, get_settings
from core.diff import FactDiff, diff_facts
from core.graph import GraphHandle, scratch_graph_name
from core.impact import ImpactCandidate, ImpactResult, compute_impact
from core.judge import JudgeVerdict, judge
from core.ledger import Ledger, changeset_id
from core.loaders.mdx import MDXLoader
from core.reanswer import Answer
from core.reanswer import answer_question as _default_answer_question

logger = logging.getLogger(__name__)

ONTOLOGY_PATH = Path(__file__).parent / "ontology" / "schema.json"

# Types for the two LLM-touching seams — see module docstring.
ApplyChangesFn = Callable[..., Awaitable[ApplyChangesResult]]
AnswerFn = Callable[[GraphRAG, GraphHandle, str], Awaitable[Answer]]
StageCallback = Callable[[str], None] | None
"""Fired with a short stage name (§10.1's SSE progress: "copy -> apply ->
diff -> impact -> re-answer -> judge -> post") — sync, fire-and-forget; the
API layer wires this to an SSE emitter. ``None`` (the default) is a no-op."""


def _emit(cb: StageCallback, stage: str) -> None:
    if cb is not None:
        cb(stage)


def load_ontology(path: Path | None = None) -> Ontology:
    data = json.loads((path or ONTOLOGY_PATH).read_text(encoding="utf-8"))
    data.pop("_comment", None)
    return Ontology.model_validate(data)


def build_rag(
    graph_name: str,
    *,
    ontology: Ontology | None = None,
    settings: Settings | None = None,
) -> GraphRAG:
    """One GraphRAG instance, bound to one graph.

    Note: the design doc's Track 03 coverage table (§2) calls for enabling
    the SDK's text-to-Cypher retrieval path (``enable_cypher=True``) as the
    "hybrid vector, keyword, and Cypher retrieval" focus. That constructor
    kwarg exists on graphrag_sdk's GitHub ``main`` branch but not yet in the
    published 1.4.0 release actually installed here (same class of drift as
    ``FalkorDBConnection.query()``'s ``expected_errors=`` — see README).
    Left out for now; add it back once the SDK release that has it ships.
    """
    settings = settings or get_settings()
    connection = ConnectionConfig(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        username=settings.falkordb_username,
        password=settings.falkordb_password,
        graph_name=graph_name,
    )
    llm = LiteLLM(model=settings.answer_model, temperature=settings.llm_temperature)
    embedder = LiteLLMEmbedder(model=settings.embed_model, dimensions=settings.embed_dimensions)
    return GraphRAG(
        connection=connection,
        llm=llm,
        embedder=embedder,
        ontology=ontology if ontology is not None else load_ontology(),
        embedding_dimension=settings.embed_dimensions,
    )


def _loader_for(path: Path) -> LoaderStrategy | None:
    """Per-extension loader override (§7.1 step 2). ``None`` lets the SDK's
    own per-extension auto-selection handle anything else (e.g. .csv tables)."""
    suffix = path.suffix.lower()
    if suffix == ".mdx":
        return MDXLoader()
    if suffix == ".md":
        return MarkdownLoader()
    return None


def _chunker_for(path: Path) -> StructuralChunking | None:
    if path.suffix.lower() in (".mdx", ".md"):
        return StructuralChunking(max_tokens=384)
    return None


# ── Bootstrap (§7.1) ────────────────────────────────────────────


@dataclass
class BootstrapSummary:
    ingested: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    """(relative_path, error message)."""
    nodes_created: int = 0
    relationships_created: int = 0
    chunks_indexed: int = 0


async def bootstrap_ingest(
    rag: GraphRAG,
    corpus_root: Path,
    doc_globs: tuple[str, ...] = ("**/*.mdx", "**/*.md"),
    *,
    max_concurrency: int = 3,
) -> BootstrapSummary:
    """§7.1 steps 1-4. ``document_id`` is each file's POSIX path relative to
    ``corpus_root`` — the same convention the PR flow uses (GitHub's PR file
    list is already repo-relative), so an answer's ``doc_ids`` line up with a
    later PR's changed-file list without any translation.

    Caller runs ``rag.finalize()`` once after this returns (§7.1 step 4 /
    the SDK's own "call finalize() once per batch" rule) — not done here,
    so a caller ingesting in several waves controls the cadence.
    """
    paths: list[Path] = []
    seen: set[Path] = set()
    for pattern in doc_globs:
        for p in sorted(corpus_root.glob(pattern)):
            if p not in seen:
                seen.add(p)
                paths.append(p)

    summary = BootstrapSummary()
    sem = asyncio.Semaphore(max_concurrency)

    async def _one(path: Path) -> None:
        rel = path.relative_to(corpus_root).as_posix()
        async with sem:
            try:
                result = await rag.ingest(
                    str(path), document_id=rel, loader=_loader_for(path), chunker=_chunker_for(path)
                )
                summary.ingested.append(rel)
                summary.nodes_created += result.nodes_created
                summary.relationships_created += result.relationships_created
                summary.chunks_indexed += result.chunks_indexed
            except Exception as exc:
                logger.warning("Bootstrap ingest failed for %s: %s", rel, exc)
                summary.failed.append((rel, str(exc)))

    await asyncio.gather(*(_one(p) for p in paths))
    logger.info(
        "Bootstrap: %d ingested, %d failed, %d nodes, %d relationships, %d chunks",
        len(summary.ingested), len(summary.failed),
        summary.nodes_created, summary.relationships_created, summary.chunks_indexed,
    )
    return summary


# ── apply_changes with a stable, explicit document_id (§7.3 step 3) ─────


async def apply_pr_changes(
    rag: GraphRAG,
    *,
    added: list[str] | None = None,
    modified: list[str] | None = None,
    deleted: list[str] | None = None,
    root: Path,
    max_concurrency: int = 3,
    update_concurrency: int = 1,
) -> ApplyChangesResult:
    """Reimplements graphrag_sdk's ``apply_changes()`` dispatch (delete ->
    update -> add, each per-file error isolated as a ``BatchEntry`` — see the
    SDK README's "Incremental Updates" section) with one difference: an
    explicit ``document_id`` per file instead of one derived from
    ``os.path.normpath(source)``.

    Why this exists instead of calling the SDK's own ``apply_changes()``:
    that method has no per-file ``document_id`` override, so it only keeps
    ``document_id`` stable across ingest/update/apply_changes calls when the
    *process's current working directory* is the repo root every time (the
    id then falls out of the relative path itself). That's process-global
    state a webhook handler running more than one PR's job at once can't
    safely depend on. Here, ``root`` is an explicit parameter and
    ``document_id`` is set directly, so this is correct regardless of
    concurrency or cwd.

    ``added``/``modified``/``deleted`` are repo-relative paths (exactly what
    a GitHub PR's file list gives); ``root`` is where their *content* has
    already been checked out locally (a real git checkout, or files written
    by the caller after fetching blobs via the GitHub API).
    """
    result = ApplyChangesResult()

    del_sem = asyncio.Semaphore(update_concurrency)

    async def _delete(rel: str) -> None:
        async with del_sem:
            try:
                r = await rag.delete_document(rel, if_missing="ignore")
                result.deleted.append(BatchEntry.ok(r))
            except Exception as exc:
                result.deleted.append(BatchEntry.fail(exc))

    await asyncio.gather(*(_delete(rel) for rel in (deleted or [])))

    mod_sem = asyncio.Semaphore(update_concurrency)

    async def _update(rel: str) -> None:
        abs_path = root / rel
        async with mod_sem:
            try:
                r = await rag.update(
                    str(abs_path),
                    document_id=rel,
                    if_missing="ingest",
                    loader=_loader_for(abs_path),
                    chunker=_chunker_for(abs_path),
                )
                result.modified.append(BatchEntry.ok(r))
            except Exception as exc:
                result.modified.append(BatchEntry.fail(exc))

    await asyncio.gather(*(_update(rel) for rel in (modified or [])))

    add_sem = asyncio.Semaphore(max_concurrency)

    async def _add(rel: str) -> None:
        abs_path = root / rel
        async with add_sem:
            try:
                r = await rag.ingest(
                    str(abs_path), document_id=rel, loader=_loader_for(abs_path), chunker=_chunker_for(abs_path)
                )
                result.added.append(BatchEntry.ok(r))
            except Exception as exc:
                result.added.append(BatchEntry.fail(exc))

    await asyncio.gather(*(_add(rel) for rel in (added or [])))

    return result


async def _default_apply_changes(
    rag: GraphRAG, *, added, modified, deleted, root, max_concurrency=3, update_concurrency=1
) -> ApplyChangesResult:
    return await apply_pr_changes(
        rag,
        added=added,
        modified=modified,
        deleted=deleted,
        root=root,
        max_concurrency=max_concurrency,
        update_concurrency=update_concurrency,
    )


async def _default_answer_fn(rag: GraphRAG, graph: GraphHandle, question: str) -> Answer:
    return await _default_answer_question(rag, graph, question)


# ── PR flow (§7.3) ───────────────────────────────────────────────


@dataclass
class ImpactReportEntry:
    answer_id: str
    question: str
    tier: str
    via: list[str]
    verdict: str
    old_answer: str
    new_answer: str
    reason: str
    score: float | None = None


@dataclass
class PRFlowReport:
    changeset_id: str
    pr: int
    base_sha: str
    head_sha: str
    doc_ids: list[str]
    facts_added: int
    facts_removed: int
    facts_modified: int
    tier_raw_counts: dict[str, int]
    truncated: bool
    entries: list[ImpactReportEntry]
    apply_result: ApplyChangesResult
    timings_ms: dict[str, int]

    @property
    def changed_count(self) -> int:
        return sum(1 for e in self.entries if e.verdict == "CHANGED")

    @property
    def now_abstains_count(self) -> int:
        return sum(1 for e in self.entries if e.verdict == "NOW_ABSTAINS")

    @property
    def now_answers_count(self) -> int:
        return sum(1 for e in self.entries if e.verdict == "NOW_ANSWERS")

    @property
    def has_coverage_regression(self) -> bool:
        """§9 / §10.2: the GitHub check run goes ``neutral``/warning exactly
        when this is True — at least one question lost all its evidence."""
        return self.now_abstains_count > 0


async def run_pr_flow(
    *,
    pr: int,
    base_sha: str,
    head_sha: str,
    added: list[str],
    modified: list[str],
    deleted: list[str],
    checkout_root: Path,
    settings: Settings | None = None,
    apply_changes_fn: ApplyChangesFn = _default_apply_changes,
    answer_fn: AnswerFn = _default_answer_fn,
    judge_llm: LiteLLM | None = None,
    on_stage: StageCallback = None,
    delete_scratch_when_done: bool = True,
) -> PRFlowReport:
    """The core PR pipeline (§7.3's sequence diagram, steps 1-9).

    ``added``/``modified``/``deleted`` should already be filtered to content
    files (*.md/*.mdx/...) by the caller (§7.3 step 1) — a PR touching none
    still produces a valid, zero-impact ``PRFlowReport``, which is the
    Replay Lab's false-positive control case (§7.5 step 1), not a special
    case handled here.

    ``apply_changes_fn`` and ``answer_fn`` are the two LLM-touching seams;
    override them in tests to keep everything else (GRAPH.COPY, fact diff,
    impact tiers, ledger writes) exercised against a real FalkorDB with zero
    LLM calls. See tests/test_ingest.py.
    """
    settings = settings or get_settings()
    doc_ids = [*added, *modified, *deleted]
    timings: dict[str, int] = {}
    t0 = time.monotonic()

    def _lap(stage: str) -> None:
        timings[stage] = int((time.monotonic() - t0) * 1000)

    main = GraphHandle(settings.main_graph, settings)
    main_ledger = Ledger(main, settings)
    cs_id = changeset_id(pr, head_sha)
    await main_ledger.set_changeset_status(cs_id, pr, head_sha, "running", "copy")

    _emit(on_stage, "copy")
    scratch_name = scratch_graph_name(pr, head_sha)
    scratch = await main.copy_to(scratch_name)
    _lap("copy")

    try:
        _emit(on_stage, "apply")
        await main_ledger.set_changeset_status(cs_id, pr, head_sha, "running", "apply")
        rag_scratch = build_rag(scratch_name, settings=settings)
        apply_result = await apply_changes_fn(
            rag_scratch, added=added, modified=modified, deleted=deleted, root=checkout_root
        )
        await rag_scratch.finalize()
        _lap("apply")

        _emit(on_stage, "diff")
        await main_ledger.set_changeset_status(cs_id, pr, head_sha, "running", "diff")
        diff: FactDiff = await diff_facts(main, scratch, doc_ids, embedder=rag_scratch.embedder, settings=settings)
        _lap("diff")

        _emit(on_stage, "impact")
        await main_ledger.set_changeset_status(cs_id, pr, head_sha, "running", "impact")
        impact: ImpactResult = await compute_impact(main, diff, doc_ids, settings=settings)
        _lap("impact")

        _emit(on_stage, "re-answer")
        await main_ledger.set_changeset_status(cs_id, pr, head_sha, "running", "re-answer")
        entries: list[ImpactReportEntry] = []
        for candidate in impact.candidates:
            entry = await _reanswer_and_judge(
                main_ledger, rag_scratch, scratch, candidate,
                answer_fn=answer_fn, judge_llm=judge_llm, settings=settings,
            )
            if entry is not None:
                entries.append(entry)
        _lap("re-answer")

        _emit(on_stage, "post")
        touches = (
            [{"doc_id": d, "op": "added"} for d in added]
            + [{"doc_id": d, "op": "modified"} for d in modified]
            + [{"doc_id": d, "op": "deleted"} for d in deleted]
        )
        impacts_payload = [
            {
                "answer_id": e.answer_id,
                "tier": e.tier,
                "via": e.via,
                "verdict": e.verdict,
                "new_answer": e.new_answer,
                "reason": e.reason,
                "score": e.score if e.score is not None else 0.0,
            }
            for e in entries
        ]
        await main_ledger.persist_changeset(
            cs_id=cs_id,
            pr=pr,
            base_sha=base_sha,
            head_sha=head_sha,
            doc_ids=doc_ids,
            status="done",
            timings=timings,
            touches=touches,
            impacts=impacts_payload,
        )
        _lap("post")

        return PRFlowReport(
            changeset_id=cs_id,
            pr=pr,
            base_sha=base_sha,
            head_sha=head_sha,
            doc_ids=doc_ids,
            facts_added=len(diff.added),
            facts_removed=len(diff.removed),
            facts_modified=len(diff.modified),
            tier_raw_counts=impact.tier_raw_counts,
            truncated=impact.truncated,
            entries=entries,
            apply_result=apply_result,
            timings_ms=timings,
        )
    except Exception:
        await main_ledger.set_changeset_status(cs_id, pr, head_sha, "failed", "error")
        raise
    finally:
        if delete_scratch_when_done:
            await scratch.delete()
        await scratch.close()


async def _reanswer_and_judge(
    ledger: Ledger,
    rag_scratch: GraphRAG,
    scratch: GraphHandle,
    candidate: ImpactCandidate,
    *,
    answer_fn: AnswerFn,
    judge_llm: LiteLLM | None,
    settings: Settings,
) -> ImpactReportEntry | None:
    info = await ledger.current_answer_for_question(candidate.answer_id)
    if info is None:
        logger.warning("Impact candidate %s has no current answer (already superseded?)", candidate.answer_id)
        return None

    old_answer = Answer(
        question=info["question_text"],
        text=info["old_answer"],
        abstained=bool(info["old_abstained"]),
        model="",
        retriever_result=None,
        resolved=None,
    )
    new_answer = await answer_fn(rag_scratch, scratch, info["question_text"])
    verdict: JudgeVerdict = await judge(
        info["question_text"], old_answer, new_answer, judge_llm=judge_llm, settings=settings
    )

    return ImpactReportEntry(
        answer_id=candidate.answer_id,
        question=info["question_text"],
        tier=candidate.tier,
        via=candidate.via,
        verdict=verdict.verdict,
        old_answer=old_answer.text,
        new_answer=new_answer.text,
        reason=verdict.reason,
        score=candidate.score,
    )


# ── Merge flow (§7.4) ─────────────────────────────────────────────


@dataclass
class MergeFlowSummary:
    pr: int
    superseded: list[tuple[str, str]] = field(default_factory=list)
    """(old_answer_id, new_answer_id) pairs actually superseded."""
    apply_result: ApplyChangesResult | None = None


async def run_merge_flow(
    *,
    pr: int,
    changeset_id_: str,
    added: list[str],
    modified: list[str],
    deleted: list[str],
    checkout_root: Path,
    settings: Settings | None = None,
    apply_changes_fn: ApplyChangesFn = _default_apply_changes,
    answer_fn: AnswerFn = _default_answer_fn,
) -> MergeFlowSummary:
    """§7.4: apply the same file set to ``docs_main`` itself, then re-answer
    (on ``docs_main`` this time, so new USED_* edges point at ``docs_main``'s
    own chunk/entity ids) every answer this PR's ChangeSet actually changed,
    and supersede the old one.

    Re-answering on main rather than promoting the scratch graph: chunk ids
    are UUIDs assigned at ingestion, so ids in ``docs_pr_...`` don't match a
    separately-ingested ``docs_main`` — re-answering the (small) impacted set
    is what keeps the ledger's ids consistent, and is cheap precisely because
    it's only the impacted set, not the whole question bank.
    """
    settings = settings or get_settings()
    main = GraphHandle(settings.main_graph, settings)
    ledger = Ledger(main, settings)

    rag_main = build_rag(settings.main_graph, settings=settings)
    apply_result = await apply_changes_fn(
        rag_main, added=added, modified=modified, deleted=deleted, root=checkout_root
    )
    await rag_main.finalize()

    rows = await main.rows_named("q21_changeset_impacts_for_merge", {"cs_id": changeset_id_})
    summary = MergeFlowSummary(pr=pr, apply_result=apply_result)

    for old_answer_id, _question_id, question_text, verdict in rows:
        new_answer = await answer_fn(rag_main, main, question_text)
        from core.ledger import AnswerWrite  # local import: avoids a cycle with reanswer's Answer type

        question_embedding = (await rag_main.embedder.aembed_documents([question_text]))[0]
        write = AnswerWrite(
            question_text=question_text,
            question_embedding=question_embedding,
            answer_text=new_answer.text,
            commit_sha="",  # filled by the caller if it tracks the merge commit sha separately
            graph_name=settings.main_graph,
            abstained=new_answer.abstained,
            fact_keys=sorted(new_answer.resolved.fact_keys) if new_answer.resolved else [],
            entity_ids=sorted(new_answer.resolved.entity_ids) if new_answer.resolved else [],
            doc_ids=sorted(new_answer.resolved.doc_ids) if new_answer.resolved else [],
            chunk_ids=sorted(new_answer.resolved.chunk_ids) if new_answer.resolved else [],
            model=new_answer.model,
        )
        _, new_answer_id = await ledger.write_answer(write)
        await ledger.supersede(old_answer_id, new_answer_id, pr, verdict)
        summary.superseded.append((old_answer_id, new_answer_id))

    return summary
