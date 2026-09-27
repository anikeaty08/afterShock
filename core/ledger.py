# Aftershock — Core: Ledger
#
# Reads and writes the Aftershock layer of the graph (§6.3): Question,
# Answer, ChangeSet nodes and the edges between them. Every method here maps
# directly to one named entry in the Cypher catalog (core/cypher/*.cypher,
# §8) — this module owns *when* those queries run and how their params are
# shaped; it never inlines Cypher of its own.

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.config import Settings, get_settings
from core.graph import GraphHandle

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_question(text: str) -> str:
    """Normalise a question's text before hashing/comparing (§6.3 Question.id
    is "sha1 of normalised text" — this is that normalisation, kept as its
    own function so ledger writes and dedup checks can't drift apart)."""
    return _WHITESPACE_RE.sub(" ", text.strip().lower())


def question_id_for(text: str) -> str:
    return hashlib.sha1(normalize_question(text).encode("utf-8")).hexdigest()


def new_answer_id() -> str:
    return str(uuid.uuid4())


def changeset_id(pr: int, head_sha: str) -> str:
    return f"pr-{pr}-{head_sha[:7]}"


@dataclass
class AnswerWrite:
    """Everything Q7/Q8 need to persist one answer. Built by reanswer.py
    (generation) + evidence.py (resolution) before ledger.write_answer()."""

    question_text: str
    question_embedding: list[float]
    answer_text: str
    commit_sha: str
    graph_name: str
    abstained: bool
    fact_keys: list[str] = field(default_factory=list)
    entity_ids: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)
    model: str = ""
    source: str = "generated"
    """One of "generated" | "curated" | "live" | "mcp" (§6.3)."""
    question_id: str | None = None
    answer_id: str | None = None


class Ledger:
    def __init__(self, graph: GraphHandle, settings: Settings | None = None) -> None:
        self.graph = graph
        self.settings = settings or get_settings()

    # ── Bootstrap ───────────────────────────────────────────────

    async def ensure_indexes(self) -> None:
        """Create the range + vector indexes from §6.3. Idempotent: FalkorDB
        raises on a duplicate index, which is expected on every run after the
        first, so it's passed as an "expected" (debug-logged, not error)
        outcome rather than treated as a real failure."""
        from core.graph import cypher_text

        # Note: the installed graphrag_sdk (1.4.0, PyPI) predates the
        # `expected_errors=` kwarg that exists on the SDK's GitHub main
        # branch — FalkorDBConnection.query() here takes no such param, so
        # "already indexed" is told apart from a real failure by matching
        # the message text ourselves instead.
        statements = [s.strip() for s in cypher_text("indexes").split("\n;\n") if s.strip()]
        for stmt in statements:
            stmt = stmt.format(dimensions=self.settings.embed_dimensions)
            try:
                await self.graph.run(stmt)
            except Exception as exc:
                if "already indexed" in str(exc).lower() or "already exists" in str(exc).lower():
                    logger.debug("Index already exists, skipping: %s", stmt[:80])
                else:
                    raise

    # ── Q7 / Q8 — write an answer ────────────────────────────────

    async def write_answer(self, w: AnswerWrite) -> tuple[str, str]:
        """Write the Question (if new) + Answer + ANSWERED_BY edge, then
        attach USED_CHUNK / USED_ENTITY evidence edges. Returns
        ``(question_id, answer_id)``.
        """
        qid = w.question_id or question_id_for(w.question_text)
        aid = w.answer_id or new_answer_id()

        await self.graph.run_named(
            "q7_write_question_answer",
            {
                "qid": qid,
                "qtext": w.question_text,
                "qemb": w.question_embedding,
                "source": w.source,
                "aid": aid,
                "answer": w.answer_text,
                "sha": w.commit_sha,
                "graph": w.graph_name,
                "abstained": w.abstained,
                "fact_keys": w.fact_keys,
                "entity_ids": w.entity_ids,
                "doc_ids": w.doc_ids,
                "model": w.model,
            },
        )

        if w.chunk_ids:
            await self.graph.run_named(
                "q8a_attach_used_chunk", {"aid": aid, "chunk_ids": w.chunk_ids}
            )
        if w.entity_ids:
            await self.graph.run_named(
                "q8b_attach_used_entity", {"aid": aid, "entity_ids": w.entity_ids}
            )
        return qid, aid

    # ── Q12 / Q17 — ChangeSet lifecycle ──────────────────────────

    async def set_changeset_status(
        self, cs_id: str, pr: int, head_sha: str, status: str, stage: str
    ) -> None:
        """Interim heartbeat for SSE progress (§10.1 GET /changesets/{id}/events).
        Q12a below overwrites status/timings again at the end with the final
        report — this just lets a client watch queued -> running -> done."""
        await self.graph.run_named(
            "q17_set_changeset_status",
            {"cs_id": cs_id, "pr": pr, "head": head_sha, "status": status, "stage": stage},
        )

    async def persist_changeset(
        self,
        *,
        cs_id: str,
        pr: int,
        base_sha: str,
        head_sha: str,
        doc_ids: list[str],
        status: str,
        timings: dict[str, Any],
        touches: list[dict[str, str]],
        impacts: list[dict[str, Any]],
    ) -> None:
        """Persist the finished PR report (§7.3 step 9): the ChangeSet node,
        its TOUCHES edges, and one IMPACTS edge per re-checked Answer."""
        await self.graph.run_named(
            "q12_persist_changeset",
            {
                "cs_id": cs_id,
                "pr": pr,
                "base": base_sha,
                "head": head_sha,
                "doc_ids": doc_ids,
                "status": status,
                "timings_json": json.dumps(timings),
            },
        )
        if touches:
            await self.graph.run_named("q12b_touches", {"cs_id": cs_id, "touches": touches})
        if impacts:
            await self.graph.run_named("q12c_impacts", {"cs_id": cs_id, "impacts": impacts})

    # ── Q13 — supersede on merge ──────────────────────────────────

    async def supersede(self, old_answer_id: str, new_answer_id: str, pr: int, verdict: str) -> None:
        await self.graph.run_named(
            "q13_supersede",
            {"old_id": old_answer_id, "new_id": new_answer_id, "pr": pr, "verdict": verdict},
        )

    # ── Q14 / Q16 / Q18 — reads ───────────────────────────────────

    async def as_of(self, question_id: str, as_of_ms: int | None = None) -> dict[str, Any] | None:
        if as_of_ms is None:
            # Ask FalkorDB for its own idea of "now" rather than stamping
            # this from the Python process's clock. created_at was written
            # with the server's timestamp() (§8 Q7); under WSL2 the VM's
            # clock can drift from the Windows host's by a few hundred ms,
            # which is enough for a host-clock "now" to land *before*
            # a created_at that is, in real wall-clock terms, already in the
            # past — silently hiding the very row this call is meant to find.
            # Comparing server-time against server-time removes the race.
            now_rows = await self.graph.rows("RETURN timestamp()")
            as_of_ms = now_rows[0][0]
        rows = await self.graph.rows_named(
            "q14_as_of", {"qid": question_id, "as_of": as_of_ms}
        )
        if not rows:
            return None
        text, sha, created_at = rows[0]
        return {"text": text, "commit_sha": sha, "created_at": created_at}

    async def current_answer_for_question(self, answer_id: str) -> dict[str, Any] | None:
        rows = await self.graph.rows_named(
            "q16_current_answer_for_question", {"answer_id": answer_id}
        )
        if not rows:
            return None
        qid, qtext, old_answer, old_abstained, fact_keys, entity_ids, doc_ids = rows[0]
        return {
            "question_id": qid,
            "question_text": qtext,
            "old_answer": old_answer,
            "old_abstained": old_abstained,
            "fact_keys": fact_keys or [],
            "entity_ids": entity_ids or [],
            "doc_ids": doc_ids or [],
        }

    async def all_current_questions(self) -> list[dict[str, Any]]:
        rows = await self.graph.rows_named("q18_all_current_questions", {})
        return [{"question_id": r[0], "question_text": r[1], "source": r[2]} for r in rows]

    async def impact_subgraph(self, cs_id: str) -> list[dict[str, Any]]:
        rows = await self.graph.rows_named("q15_impact_subgraph", {"cs_id": cs_id})
        return [
            {
                "question": r[0],
                "old_answer": r[1],
                "new_answer": r[2],
                "tier": r[3],
                "verdict": r[4],
                "via": r[5],
                "docs": r[6],
            }
            for r in rows
        ]
