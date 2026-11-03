import pytest
from fastapi.testclient import TestClient

import finance_agent.api as api

BODY = {
    "id": "chat-1",
    "trigger": "submit-message",
    "messages": [{"id": "u1", "role": "user", "parts": [{"type": "text", "text": "hello"}]}],
}


def test_chat_streams_ai_sdk_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCE_AGENT_CHAT_MODEL", "test")

    response = TestClient(api.app).post("/api/chat", json=BODY)

    assert response.status_code == 200
    assert '"type":"text-delta"' in response.text.replace(" ", "")
    assert "data: [DONE]" in response.text


def test_chat_reports_missing_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FINANCE_AGENT_CHAT_MODEL", "anthropic:claude-haiku-4-5")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("finance_agent.chat.load_dotenv", lambda *a, **k: None)

    response = TestClient(api.app).post("/api/chat", json=BODY)

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]
