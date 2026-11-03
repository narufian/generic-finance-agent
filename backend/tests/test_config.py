from pathlib import Path

import pytest

from finance_agent.config import (
    DEFAULT_AGENT_REQUEST_LIMIT,
    DEFAULT_FAST_REVIEW_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_ORCHESTRATOR_MODEL,
    DEFAULT_REVIEW_MODEL,
    load_live_settings,
)


def test_load_live_settings_reads_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "E2B_API_KEY",
        "FINANCE_AGENT_MODEL",
        "FINANCE_AGENT_OPUS_MODEL",
        "FINANCE_AGENT_REVIEW_MODEL",
        "FINANCE_AGENT_FAST_REVIEW_MODEL",
        "FINANCE_AGENT_AGENT_REQUEST_LIMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=test-anthropic\nE2B_API_KEY=test-e2b\n")

    settings = load_live_settings(env_file=env_file)

    assert settings.orchestrator_model == DEFAULT_ORCHESTRATOR_MODEL
    assert settings.opus_model == DEFAULT_OPUS_MODEL
    assert settings.reviewer_model == DEFAULT_REVIEW_MODEL
    assert settings.fast_reviewer_model == DEFAULT_FAST_REVIEW_MODEL
    assert settings.agent_request_limit == DEFAULT_AGENT_REQUEST_LIMIT


def test_load_live_settings_reads_agent_request_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("E2B_API_KEY", "test-e2b")
    monkeypatch.setenv("FINANCE_AGENT_AGENT_REQUEST_LIMIT", "350")

    settings = load_live_settings(env_file=tmp_path / "missing.env")

    assert settings.agent_request_limit == 350


def test_load_live_settings_reads_fast_review_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("E2B_API_KEY", "test-e2b")
    monkeypatch.setenv("FINANCE_AGENT_FAST_REVIEW_MODEL", "anthropic:test-haiku")

    settings = load_live_settings(env_file=tmp_path / "missing.env")

    assert settings.fast_reviewer_model == "anthropic:test-haiku"


def test_load_live_settings_reads_opus_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic")
    monkeypatch.setenv("E2B_API_KEY", "test-e2b")
    monkeypatch.setenv("FINANCE_AGENT_OPUS_MODEL", "anthropic:test-opus")

    settings = load_live_settings(env_file=tmp_path / "missing.env")

    assert settings.opus_model == "anthropic:test-opus"


def test_load_live_settings_reports_missing_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="E2B_API_KEY, ANTHROPIC_API_KEY"):
        load_live_settings(env_file=tmp_path / "missing.env")


def test_load_live_settings_requires_only_configured_provider_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("E2B_API_KEY", "test-e2b")
    for name in (
        "FINANCE_AGENT_MODEL",
        "FINANCE_AGENT_OPUS_MODEL",
        "FINANCE_AGENT_REVIEW_MODEL",
        "FINANCE_AGENT_TRIAGE_MODEL",
        "FINANCE_AGENT_FAST_REVIEW_MODEL",
        "FINANCE_AGENT_REPORT_REVIEW_MODEL",
    ):
        monkeypatch.setenv(name, "openrouter:anthropic/claude-sonnet-4.6")

    with pytest.raises(RuntimeError, match="^Missing required environment variables: OPENROUTER_API_KEY$"):
        load_live_settings(env_file=tmp_path / "missing.env")

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter")
    settings = load_live_settings(env_file=tmp_path / "missing.env")
    assert settings.orchestrator_model.startswith("openrouter:")
