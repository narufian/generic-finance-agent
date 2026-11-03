"""Casual chat endpoint streamed to the AI SDK `useChat` frontend."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic_ai import Agent

from finance_agent.config import DEFAULT_ORCHESTRATOR_MODEL, PROJECT_ROOT, PROVIDER_API_KEYS

CHAT_INSTRUCTIONS = (
    "You are a friendly, concise assistant for finance and investment questions. "
    "Answer conversationally in Markdown. When you are unsure of a fact or figure, say so "
    "rather than guessing, and never present an estimate as a reported number."
)


def chat_model() -> str:
    """Resolve the chat model and check its provider key. Only the model key is needed, not E2B."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    model = os.getenv("FINANCE_AGENT_CHAT_MODEL") or os.getenv(
        "FINANCE_AGENT_MODEL", DEFAULT_ORCHESTRATOR_MODEL
    )
    key = PROVIDER_API_KEYS.get(model.split(":", 1)[0])
    if key and not os.getenv(key):
        raise RuntimeError(f"Missing required environment variable: {key}")
    return model


def build_chat_agent() -> Agent:
    # Built per request so edits to .env apply without restarting the server.
    return Agent(chat_model(), instructions=CHAT_INSTRUCTIONS)
