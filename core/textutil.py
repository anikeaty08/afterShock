# Aftershock — Core: small text helpers shared by evidence.py and diff.py.

from __future__ import annotations

import re

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text.strip())


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain-Python cosine similarity — diff.py's embedder is optional (no
    API key in a test/offline run), so this avoids a numpy import just for
    two short vectors compared occasionally during a fact diff."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
