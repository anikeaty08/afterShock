# Aftershock — Core: Evidence resolver
#
# The most important piece of glue in the whole system (SYSTEM_DESIGN.md
# §7.2). Under the default MultiPathRetrieval strategy, a completion's
# retriever_result.items are whole concatenated *sections* — "facts",
# "relationships", "entities", "passages", "cypher_results", "hint" — not
# individual chunk/entity ids. This module turns that prose back into the
# chunk ids, entity ids, fact keys and document ids an Answer node needs
# (Q7-Q10 in the Cypher catalog), so the impact tiers in impact.py have
# something to match against.
#
# The exact string formats parsed here (the em-dash arrow "src —[REL]→ tgt:
# fact" and the "[Source: path]\ntext" passage prefix) come straight from
# graphrag_sdk's retrieval/strategies/{entity_discovery,relationship_expansion,
# multi_path}.py — verified against the installed 1.4.0 source, not guessed
# (the Day-1 spike SYSTEM_DESIGN.md §16 calls for). If a future SDK version
# changes those formats, the tests in tests/test_evidence.py break loudly
# instead of this module silently resolving nothing.

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from graphrag_sdk.core.models import RetrieverResult

from core.graph import GraphHandle

logger = logging.getLogger(__name__)

# Sections that carry no document/fact evidence at all — never worth a
# lookup. Matches the SDK's own grounded-abstention example's
# NON_EVIDENCE_SECTIONS, plus "cypher_results" (rows from the optional
# text-to-Cypher path, already an aggregate — nothing chunk/fact-shaped to
# resolve back to).
NON_EVIDENCE_SECTIONS = frozenset({"hint", "cypher_results"})

_HEADING_LINE_RE = re.compile(r"^##[^\n]*\n?", re.MULTILINE)
_HOP_SPLIT_RE = re.compile(r"—\[([^\]]+)\]→")
_SOURCE_PREFIX_RE = re.compile(r"^\[Source:\s*(.+?)\]\n(.*)$", re.DOTALL)
_PASSAGE_PREFIX_CHARS = 120
"""§7.2 Q9: match a passage back to its chunk on its first ~120 normalised
chars — long enough to be a near-unique fingerprint, short enough to survive
minor whitespace differences between the retrieved and stored text."""


@dataclass
class Hop:
    src_name: str
    rel_type: str
    tgt_name: str
    fact_text: str | None = None


@dataclass
class ResolvedEvidence:
    """Chunk/entity/fact/document ids an Answer's context resolves to.

    Feeds directly into Q7/Q8's ``fact_keys`` / ``entity_ids`` / ``doc_ids``
    Answer properties and the USED_CHUNK / USED_ENTITY edges.
    """

    chunk_ids: set[str] = field(default_factory=set)
    entity_ids: set[str] = field(default_factory=set)
    fact_keys: set[str] = field(default_factory=set)
    doc_ids: set[str] = field(default_factory=set)
    unresolved_hops: list[Hop] = field(default_factory=list)
    """Hops whose fact_key lookup (Q10) missed — usually re-extraction
    variance between the passage's displayed name and the stored entity name.
    Kept for observability, not fatal: recall degrades gracefully, doesn't
    error out. See §16 risk: "context-section formats are hard to parse"."""


def parse_hop_line(line: str) -> list[Hop]:
    """Parse one "src —[REL]→ tgt[ —[REL2]→ tgt2 [...]][: fact]" line.

    Handles both the 1-hop form (which may carry a trailing ": fact_text",
    per entity_discovery.search_relates_edges / relationship_expansion's
    1-hop block) and the 2-hop form (which never does — see
    relationship_expansion.expand_relationships's 2-hop block). Returns one
    ``Hop`` per edge in the chain, in order.
    """
    line = line.strip().lstrip("-").strip()
    if not line:
        return []
    parts = _HOP_SPLIT_RE.split(line)
    # An n-hop line splits into 2n+1 parts: [entity, rel, entity, rel, ...].
    if len(parts) < 3 or len(parts) % 2 == 0:
        return []
    entities = parts[0::2]
    rels = parts[1::2]
    hops: list[Hop] = []
    last = len(rels) - 1
    for i, rel in enumerate(rels):
        src = entities[i].strip()
        tgt_raw = entities[i + 1].strip()
        fact_text = None
        if i == last and ": " in tgt_raw:
            tgt, fact_text = tgt_raw.split(": ", 1)
            tgt = tgt.strip()
            fact_text = fact_text.strip() or None
        else:
            tgt = tgt_raw
        if src and rel.strip() and tgt:
            hops.append(Hop(src_name=src, rel_type=rel.strip(), tgt_name=tgt, fact_text=fact_text))
    return hops


def parse_entity_lines(content: str) -> list[str]:
    """Parse a "## Key Entities\n- Name: desc\n- Name2" section into names."""
    body = _HEADING_LINE_RE.sub("", content, count=1)
    names: list[str] = []
    for line in body.splitlines():
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        name = line.split(":", 1)[0].strip()
        if name:
            names.append(name)
    return names


def parse_passage_blocks(content: str) -> list[tuple[str | None, str]]:
    """Parse a "## Source Document Passages\n<p1>\n---\n<p2>..." section into
    (document_path_or_None, passage_text) pairs. A passage without a
    "[Source: ...]" prefix means the SDK couldn't map it to a document
    (``text_to_doc`` miss) — kept with ``None`` so callers can still use the
    text, just not attribute it to a document.
    """
    body = _HEADING_LINE_RE.sub("", content, count=1)
    blocks = body.split("\n---\n")
    out: list[tuple[str | None, str]] = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = _SOURCE_PREFIX_RE.match(block)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
        else:
            out.append((None, block))
    return out


async def resolve_evidence(graph: GraphHandle, retriever_result: RetrieverResult) -> ResolvedEvidence:
    """Turn a completion's retrieved context back into ledger-writable ids.

    Best-effort throughout: a passage or fact line that fails to resolve is
    dropped with a debug log, never raised — an Answer with partial evidence
    is still far more useful than no Answer at all, and §16 flags this exact
    parsing step as the riskiest part of the design.
    """
    resolved = ResolvedEvidence()

    for item in retriever_result.items:
        section = (item.metadata or {}).get("section", "")
        if section in NON_EVIDENCE_SECTIONS:
            continue

        if section == "passages":
            for source, passage_text in parse_passage_blocks(item.content):
                if not source:
                    continue
                # Raw prefix, not whitespace-normalised: Q9 is an exact
                # CONTAINS against the stored chunk text.
                prefix = passage_text.strip()[:_PASSAGE_PREFIX_CHARS]
                try:
                    rows = await graph.rows_named(
                        "q9_resolve_passage_to_chunk",
                        {"source": source, "passage_prefix": prefix},
                    )
                except Exception:
                    logger.debug("Q9 passage resolution failed for %s", source, exc_info=True)
                    continue
                if rows:
                    doc_id, chunk_id = rows[0]
                    resolved.doc_ids.add(doc_id)
                    if chunk_id:
                        resolved.chunk_ids.add(chunk_id)

        elif section in ("facts", "relationships"):
            body = _HEADING_LINE_RE.sub("", item.content, count=1)
            for line in body.splitlines():
                for hop in parse_hop_line(line):
                    try:
                        rows = await graph.rows_named(
                            "q10_resolve_fact_line",
                            {"src": hop.src_name, "rel": hop.rel_type, "tgt": hop.tgt_name},
                        )
                    except Exception:
                        logger.debug("Q10 fact resolution failed for %r", hop, exc_info=True)
                        rows = []
                    if rows:
                        fact_key, src_id, tgt_id = rows[0][0], rows[0][1], rows[0][2]
                        resolved.fact_keys.add(fact_key)
                        resolved.entity_ids.add(src_id)
                        resolved.entity_ids.add(tgt_id)
                    else:
                        resolved.unresolved_hops.append(hop)

        elif section == "entities":
            for name in parse_entity_lines(item.content):
                try:
                    rows = await graph.rows(
                        "MATCH (e:__Entity__ {name: $name}) RETURN e.id AS id",
                        {"name": name},
                    )
                except Exception:
                    logger.debug("Entity name resolution failed for %r", name, exc_info=True)
                    continue
                for row in rows:
                    resolved.entity_ids.add(row[0])

    if resolved.unresolved_hops:
        logger.info(
            "Evidence resolver: %d/%d fact hops unresolved",
            len(resolved.unresolved_hops),
            len(resolved.unresolved_hops) + len(resolved.fact_keys),
        )

    return resolved
