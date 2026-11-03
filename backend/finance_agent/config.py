"""Local configuration for live agent runs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ORCHESTRATOR_MODEL = "anthropic:claude-sonnet-4-6"
DEFAULT_OPUS_MODEL = "anthropic:claude-opus-4-6"
# Haiku rejected ~1 in 4 correct findings in testing, often citing facts not in the evidence.
DEFAULT_REVIEW_MODEL = "anthropic:claude-sonnet-4-6"
DEFAULT_TRIAGE_MODEL = "anthropic:claude-haiku-4-5"
DEFAULT_FAST_REVIEW_MODEL = "anthropic:claude-haiku-4-5"
DEFAULT_AGENT_REQUEST_LIMIT = 200

# API key each PydanticAI provider prefix needs. Providers absent here (e.g. `bedrock`,
# `google-vertex`) resolve credentials through their own SDK chain, so nothing is checked.
PROVIDER_API_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "google-gla": "GOOGLE_API_KEY",
}


@dataclass(frozen=True)
class LiveSettings:
    orchestrator_model: str
    opus_model: str
    reviewer_model: str
    triage_model: str
    report_reviewer_model: str
    fast_reviewer_model: str
    agent_request_limit: int


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer") from error
    if value < 1:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def load_live_settings(*, env_file: Path | None = None) -> LiveSettings:
    """Load the repository's .env and validate credentials for a live run."""
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    settings = LiveSettings(
        orchestrator_model=os.getenv("FINANCE_AGENT_MODEL", DEFAULT_ORCHESTRATOR_MODEL),
        opus_model=os.getenv("FINANCE_AGENT_OPUS_MODEL", DEFAULT_OPUS_MODEL),
        reviewer_model=os.getenv("FINANCE_AGENT_REVIEW_MODEL", DEFAULT_REVIEW_MODEL),
        triage_model=os.getenv("FINANCE_AGENT_TRIAGE_MODEL", DEFAULT_TRIAGE_MODEL),
        fast_reviewer_model=os.getenv(
            "FINANCE_AGENT_FAST_REVIEW_MODEL", DEFAULT_FAST_REVIEW_MODEL
        ),
        # The whole-report review needs judgment, so it defaults to the orchestrator's model.
        report_reviewer_model=os.getenv(
            "FINANCE_AGENT_REPORT_REVIEW_MODEL",
            os.getenv("FINANCE_AGENT_MODEL", DEFAULT_ORCHESTRATOR_MODEL),
        ),
        agent_request_limit=_positive_int(
            "FINANCE_AGENT_AGENT_REQUEST_LIMIT", DEFAULT_AGENT_REQUEST_LIMIT
        ),
    )

    models = (
        settings.orchestrator_model,
        settings.opus_model,
        settings.reviewer_model,
        settings.triage_model,
        settings.report_reviewer_model,
        settings.fast_reviewer_model,
    )
    required = ["E2B_API_KEY"]
    for model in models:
        key = PROVIDER_API_KEYS.get(model.split(":", 1)[0])
        if key and key not in required:
            required.append(key)
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )
    return settings
