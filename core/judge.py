# Aftershock — Core: Re-answer judge
#
# §9. Two stages, cheapest first: a deterministic pass that needs no LLM call
# at all, then an LLM judge — temperature 0, a **different model family**
# from the answer model (Settings.judge_model vs Settings.answer_model) so
# the judge never grades a model against itself.

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from graphrag_sdk import LiteLLM

from core.config import Settings, get_settings
from core.reanswer import Answer
from core.textutil import normalize_whitespace

logger = logging.getLogger(__name__)

_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\n(.*?)```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

VALID_LLM_VERDICTS = frozenset({"UNCHANGED", "REWORDED", "CHANGED"})
"""Q12c's ``verdict`` column takes these plus the two deterministic-only
verdicts below — kept as separate constants because only these three are
ever the *LLM's* decision to get right or wrong (§7.5 step 4 judge
calibration is measured against this set)."""

_JUDGE_PROMPT = """You are comparing two answers to the SAME question, generated from two versions of a documentation knowledge graph (before and after a pull request). Decide whether the underlying facts changed.

Question: {question}

Answer A (before the PR):
{old_answer}

Answer B (after the PR):
{new_answer}

Rules:
- REWORDED: the two answers state the same facts, API names, parameters, defaults and steps — only the wording differs. This is NOT reported to the user as a change.
- CHANGED: at least one factual claim, API name, parameter, default value, or step differs between A and B.
- UNCHANGED: the answers are effectively identical (this case is normally caught before you're asked, but say so if you see it).

Respond with ONLY a JSON object, no markdown fences, no commentary:
{{"verdict": "UNCHANGED" | "REWORDED" | "CHANGED", "changed_claims": [{{"old": "...", "new": "..."}}], "reason": "one sentence"}}
``changed_claims`` is a list of the specific claims that differ (empty list if verdict is not CHANGED)."""

_CLAIM_CHECK_PROMPT = """You are fact-checking an AI-generated answer against the exact evidence it was given. List every sentence or claim in the ANSWER that is NOT directly supported by the CONTEXT below — claims the model appears to have added on its own.

CONTEXT:
{context}

ANSWER:
{answer}

Respond with ONLY a JSON object, no markdown fences, no commentary:
{{"unsupported_claims": ["...", "..."]}}
Return an empty list if every claim in the answer is supported by the context."""


@dataclass
class JudgeVerdict:
    verdict: str
    """One of UNCHANGED | REWORDED | CHANGED | NOW_ABSTAINS | NOW_ANSWERS."""
    reason: str
    changed_claims: list[dict[str, str]] = field(default_factory=list)
    stage: str = "deterministic"
    """"deterministic" or "llm" — which stage produced this verdict; the
    Replay Lab's judge-calibration step (§7.5 step 4) only hand-labels the
    "llm" ones, since the deterministic ones aren't a judgment call."""


def _normalize_answer(text: str) -> str:
    return normalize_whitespace(text.lower())


def _extract_code_blocks(text: str) -> list[str]:
    return [normalize_whitespace(m) for m in _CODE_FENCE_RE.findall(text)]


def code_blocks_differ(old_text: str, new_text: str) -> bool:
    """§9 stage 1's "strong hint for stage 2": fenced code differs between
    the two answers. Not a verdict on its own — surfaced to the stage-2
    prompt context via the caller, and useful for the Replay Lab to report
    separately (a code-level change is rarely a REWORDED false alarm)."""
    return _extract_code_blocks(old_text) != _extract_code_blocks(new_text)


def stage1_deterministic(old: Answer, new: Answer) -> JudgeVerdict | None:
    """§9 stage 1. Returns a final verdict for the three cases that need no
    LLM call, or ``None`` when stage 2 must run."""
    if old.abstained and new.abstained:
        return JudgeVerdict("UNCHANGED", "Both the old and new answers abstain.", stage="deterministic")
    if not old.abstained and new.abstained:
        return JudgeVerdict(
            "NOW_ABSTAINS",
            "The new graph no longer has enough evidence to answer this question.",
            stage="deterministic",
        )
    if old.abstained and not new.abstained:
        return JudgeVerdict(
            "NOW_ANSWERS",
            "The new graph now has evidence for a question that was previously unanswerable.",
            stage="deterministic",
        )
    if _normalize_answer(old.text) == _normalize_answer(new.text):
        return JudgeVerdict("UNCHANGED", "Answer text is unchanged after normalisation.", stage="deterministic")
    return None


def _parse_json_object(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Judge did not return parseable JSON: {raw[:300]!r}")


async def judge(
    question: str,
    old: Answer,
    new: Answer,
    *,
    judge_llm: LiteLLM | None = None,
    settings: Settings | None = None,
) -> JudgeVerdict:
    """Run stage 1, then stage 2 only if stage 1 didn't already decide."""
    deterministic = stage1_deterministic(old, new)
    if deterministic is not None:
        return deterministic

    settings = settings or get_settings()
    llm = judge_llm or LiteLLM(model=settings.judge_model, temperature=settings.llm_temperature)

    prompt = _JUDGE_PROMPT.format(question=question, old_answer=old.text, new_answer=new.text)
    response = await llm.ainvoke(prompt)
    try:
        parsed = _parse_json_object(response.content or "")
        verdict = str(parsed.get("verdict", "")).upper()
        if verdict not in VALID_LLM_VERDICTS:
            raise ValueError(f"Judge returned an unrecognised verdict: {verdict!r}")
        return JudgeVerdict(
            verdict=verdict,
            reason=str(parsed.get("reason", "")),
            changed_claims=list(parsed.get("changed_claims", [])),
            stage="llm",
        )
    except (ValueError, TypeError) as exc:
        # A judge that can't be parsed is not a judge that gets to decide
        # silently: fail safe towards CHANGED (§4.2 — recall over precision,
        # false positives cost money not credibility) rather than either
        # crashing the whole PR run or quietly reporting UNCHANGED.
        logger.warning("Judge response unparseable, defaulting to CHANGED: %s", exc)
        return JudgeVerdict(
            verdict="CHANGED",
            reason="Judge response could not be parsed; reported as changed out of caution.",
            stage="llm",
        )


async def check_unsupported_claims(
    answer_text: str,
    context: str,
    *,
    judge_llm: LiteLLM | None = None,
    settings: Settings | None = None,
) -> list[str]:
    """§9's claim check: statements in ``answer_text`` not supported by the
    exact evidence (``context`` — the same joined context string the answer
    was generated from). Called by the caller only for a non-abstained
    answer; an ⚠ badge in the report per unsupported claim found.
    """
    settings = settings or get_settings()
    llm = judge_llm or LiteLLM(model=settings.judge_model, temperature=settings.llm_temperature)
    prompt = _CLAIM_CHECK_PROMPT.format(context=context, answer=answer_text)
    response = await llm.ainvoke(prompt)
    try:
        parsed = _parse_json_object(response.content or "")
        claims = parsed.get("unsupported_claims", [])
        return [str(c) for c in claims] if isinstance(claims, list) else []
    except (ValueError, TypeError) as exc:
        logger.warning("Claim check response unparseable, reporting no findings: %s", exc)
        return []
