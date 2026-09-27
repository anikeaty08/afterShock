# Aftershock — Core: Settings
#
# Single source of truth for every environment-driven knob in the backend.
# Nothing here reaches FalkorDB or an LLM by itself; it's plain configuration
# read once at process start. See SYSTEM_DESIGN.md §5 (tech stack), §7.5
# (Replay Lab thresholds) and §14 (cost control) for where each value is used.

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── FalkorDB ────────────────────────────────────────────────
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: str | None = None
    falkordb_password: str | None = None

    main_graph: str = "docs_main"
    """The one long-lived graph: SDK layer + Aftershock ledger layer (§4.1)."""

    # ── LLM (via LiteLLM) ───────────────────────────────────────
    # Two different model families for answer vs judge (§5, §9) so the judge
    # never grades a model against itself. Provider prefix follows LiteLLM's
    # ``provider/model`` convention; swap freely as long as the two differ.
    answer_model: str = "bedrock/global.anthropic.claude-sonnet-4-6"
    judge_model: str = "bedrock/global.amazon.nova-2-lite-v1:0"
    embed_model: str = "bedrock/amazon.titan-embed-text-v2:0"
    extract_model: str = "bedrock/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    """Entity/relation extraction during ingest — by far the highest-volume
    LLM use (several calls per chunk, every page), so a small fast model per
    §12 ("a small, cheap model for extraction"). Answers use answer_model."""
    embed_dimensions: int = 256

    ner_backend: str = "llm"
    """Entity-recognition step of extraction: "llm" (LLMExtractor, the answer
    model) or "gliner" (the SDK's default local GLiNER model via torch).
    Defaults to "llm": GLiNER needs torch's native DLLs, which a Windows
    Application Control policy blocks on the dev machine this was built on
    (every chunk's NER step failed), and it keeps ingestion dependency-free
    of a local model download."""

    llm_temperature: float = 0.0
    """Design §9 / §7.5: temperature 0 everywhere so ground truth is stable."""

    # ── Abstention gate (§7.2) ───────────────────────────────────
    # MultiPathRetrieval's CosineReranker scores whole concatenated sections,
    # not individual chunks (see the SDK's grounded-abstention example) — this
    # floor is section-level and must be calibrated on this corpus, not
    # borrowed from a chunk-level threshold. Start conservative; the Replay
    # Lab (§7.5) tunes it against measured section scores.
    min_evidence_items: int = 1
    min_evidence_score: float = 0.30

    # ── Impact tiers (§4.2, §8) ───────────────────────────────────
    hub_degree_percentile: float = 0.95
    """Q11: entities above this RELATES-degree percentile are hub-dampened in T3."""

    t3_semantic_gate_theta: float = 0.55
    """θ₃ — keep a T3 candidate only if its question embedding is within this
    cosine similarity of at least one changed fact embedding."""

    t4_similarity_theta: float = 0.55
    """θ₄ — reverse-retrieval (Q6) similarity floor. FalkorDB's vector index
    returns a cosine *distance*; callers convert with ``1 - distance`` before
    comparing against this threshold."""

    fact_text_similarity_theta: float = 0.90
    """Facts diff (§7.3 step 4): same fact_key counts as unchanged text when
    cosine similarity of the two fact strings is at or above this — filters
    re-extraction rephrasing noise, not real content changes."""

    max_rechecks_per_pr: int = 60
    """§7.3 step 6 budget cap. The PR comment must say so if this truncated
    the flagged set."""

    # ── GitHub App (§10.2) ─────────────────────────────────────
    github_app_id: str | None = None
    github_private_key: str | None = Field(default=None, repr=False)
    """PEM contents (not a path) — set via env so no key file touches disk
    in a container image. Use ``github_private_key_path`` for local dev."""
    github_private_key_path: str | None = None
    github_webhook_secret: str | None = Field(default=None, repr=False)
    github_api_base: str = "https://api.github.com"

    # ── API service ───────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    ask_rate_limit_per_minute: int = 30
    """§14: rate-limit /ask."""

    demo_mode: bool = False
    """§13: judges without API keys still get a seeded ledger + cached asks."""

    log_level: str = "INFO"

    def github_private_key_pem(self) -> str | None:
        """Resolve the PEM either from the inline env var or a file path."""
        if self.github_private_key:
            return self.github_private_key
        if self.github_private_key_path:
            from pathlib import Path

            return Path(self.github_private_key_path).read_text(encoding="utf-8")
        return None


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _export_provider_env() -> None:
    """LiteLLM reads provider credentials (AWS_BEARER_TOKEN_BEDROCK,
    AWS_REGION_NAME, OPENAI_API_KEY, ...) from os.environ, but
    pydantic-settings only maps .env keys onto Settings fields — it never
    exports the rest. Load .env into the process environment too, without
    overriding anything already set there (a real deployment's env wins)."""
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton. Tests override via monkeypatching env
    vars *before* first call, or by constructing ``Settings(...)`` directly."""
    return Settings()


# At import, not inside get_settings(): code that constructs Settings(...)
# directly (tests, scripts) must see provider credentials too.
_export_provider_env()
