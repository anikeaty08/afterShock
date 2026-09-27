# Tests for core/judge.py. Stage 1 needs no LLM at all. Stage 2 is tested
# with a fake LLM (no API key / network needed) to verify our own JSON
# parsing and fail-safe behaviour — the actual judge model's judgment
# quality is what SYSTEM_DESIGN.md §7.5 step 4's human-calibration set
# measures, not something a unit test can check.

from __future__ import annotations

from dataclasses import dataclass

from core.judge import JudgeVerdict, check_unsupported_claims, code_blocks_differ, judge, stage1_deterministic
from core.reanswer import INSUFFICIENT_EVIDENCE, Answer


def _answer(text: str, abstained: bool = False) -> Answer:
    return Answer(question="q", text=text, abstained=abstained, model="m", retriever_result=None, resolved=None)


class TestStage1Deterministic:
    def test_both_abstained_is_unchanged(self):
        v = stage1_deterministic(_answer(INSUFFICIENT_EVIDENCE, True), _answer(INSUFFICIENT_EVIDENCE, True))
        assert v is not None and v.verdict == "UNCHANGED" and v.stage == "deterministic"

    def test_answered_then_abstained_is_now_abstains(self):
        v = stage1_deterministic(_answer("Yes."), _answer(INSUFFICIENT_EVIDENCE, True))
        assert v is not None and v.verdict == "NOW_ABSTAINS"

    def test_abstained_then_answered_is_now_answers(self):
        v = stage1_deterministic(_answer(INSUFFICIENT_EVIDENCE, True), _answer("Yes."))
        assert v is not None and v.verdict == "NOW_ANSWERS"

    def test_identical_text_is_unchanged(self):
        v = stage1_deterministic(_answer("Yes,  it does."), _answer("yes, it does."))
        assert v is not None and v.verdict == "UNCHANGED"

    def test_different_text_falls_through_to_stage_2(self):
        v = stage1_deterministic(_answer("Use KnowledgeGraph."), _answer("Use GraphRAG."))
        assert v is None


class TestCodeBlocksDiffer:
    def test_same_code_different_prose_is_not_a_diff(self):
        old = "Call it like this:\n```python\nrag.ingest(x)\n```\nDone."
        new = "You can call it like so:\n```python\nrag.ingest(x)\n```\nThat's it."
        assert code_blocks_differ(old, new) is False

    def test_changed_identifier_in_code_is_a_diff(self):
        old = "```python\nKnowledgeGraph(x)\n```"
        new = "```python\nGraphRAG(x)\n```"
        assert code_blocks_differ(old, new) is True


@dataclass
class _FakeResponse:
    content: str


class _FakeLLM:
    def __init__(self, content: str):
        self._content = content
        self.calls = 0

    async def ainvoke(self, prompt, **kwargs):
        self.calls += 1
        return _FakeResponse(self._content)


class TestStage2LLMJudge:
    async def test_parses_clean_json(self):
        fake = _FakeLLM('{"verdict": "CHANGED", "changed_claims": [{"old": "A", "new": "B"}], "reason": "API renamed"}')
        v = await judge("q?", _answer("Use A."), _answer("Use B."), judge_llm=fake)
        assert v.verdict == "CHANGED"
        assert v.stage == "llm"
        assert v.changed_claims == [{"old": "A", "new": "B"}]
        assert fake.calls == 1

    async def test_parses_json_wrapped_in_markdown_fence(self):
        fake = _FakeLLM('Here you go:\n```json\n{"verdict": "REWORDED", "changed_claims": [], "reason": "same facts"}\n```')
        v = await judge("q?", _answer("Use A to do X."), _answer("You can use A to do X."), judge_llm=fake)
        assert v.verdict == "REWORDED"

    async def test_stage1_shortcuts_before_any_llm_call(self):
        fake = _FakeLLM("should never be read")
        v = await judge("q?", _answer("same"), _answer("same"), judge_llm=fake)
        assert v.stage == "deterministic"
        assert fake.calls == 0

    async def test_unparseable_response_fails_safe_to_changed(self):
        fake = _FakeLLM("I cannot help with that request.")
        v = await judge("q?", _answer("Use A."), _answer("Use B."), judge_llm=fake)
        assert v.verdict == "CHANGED"
        assert v.stage == "llm"

    async def test_unrecognised_verdict_value_fails_safe_to_changed(self):
        fake = _FakeLLM('{"verdict": "MAYBE", "reason": "unsure"}')
        v = await judge("q?", _answer("Use A."), _answer("Use B."), judge_llm=fake)
        assert v.verdict == "CHANGED"


class TestClaimCheck:
    async def test_returns_parsed_claims(self):
        fake = _FakeLLM('{"unsupported_claims": ["The default timeout is 30s."]}')
        claims = await check_unsupported_claims("A. The default timeout is 30s.", "context with no timeout info", judge_llm=fake)
        assert claims == ["The default timeout is 30s."]

    async def test_unparseable_response_returns_empty_not_an_exception(self):
        fake = _FakeLLM("not json at all")
        claims = await check_unsupported_claims("A.", "context", judge_llm=fake)
        assert claims == []
