"""Inbox contract and ChatAgent reply path. GAME HTTP is mocked."""

import json

import pytest
from fastapi.testclient import TestClient

import app


class FakeResponse:
    def __init__(self, message, is_finished=False):
        self.message = message
        self.is_finished = is_finished


class FakeChat:
    def __init__(self, conversation_id, client=None, action_space=None, get_state_fn=None):
        self.chat_id = conversation_id
        self.client = client
        self.messages = []
        self.ended = False
        self.fail_times = 0

    def next(self, message):
        self.messages.append(message)
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("conversation expired")
        return FakeResponse(f"Lenses on {message}", is_finished=self.chat_id.endswith("-done"))

    def end(self, message=None):
        self.ended = True


class FakeAgent:
    created = []

    def __init__(self, api_key, prompt):
        assert api_key.startswith("apt-")
        assert "BigBrain2Bot" in prompt
        assert "96114" in prompt
        self.client = {"api_key": api_key}
        self.prompt = prompt

    def create_chat(self, partner_id, partner_name, action_space=None, get_state_fn=None):
        chat = FakeChat(f"conv-{len(FakeAgent.created) + 1}")
        FakeAgent.created.append((partner_id, partner_name, chat))
        return chat


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BBA_CHAT_DATA", str(tmp_path))
    monkeypatch.setenv("BBA_CHAT_SECRET", "test-secret")
    monkeypatch.delenv("GAME_API_KEY", raising=False)
    FakeAgent.created = []
    app.reset_runtime()
    monkeypatch.setattr(app, "ChatAgent", FakeAgent)
    monkeypatch.setattr(app, "Chat", FakeChat)
    with TestClient(app.app) as test_client:
        yield test_client
    app.reset_runtime()


def test_missing_key_stores_agent_error(client):
    sent = client.post("/send", json={"text": "How do the three lenses work?", "user": "analysis-app"})
    assert sent.status_code == 200
    body = sent.json()
    assert body["status"] == "ok"
    assert set(body["message"]) == {"id", "ts", "role", "text", "user"}
    assert body["message"]["role"] == "user"
    assert body["message"]["user"] == "analysis-app"

    listed = client.get("/messages", params={"since": 0}).json()
    roles = [item["role"] for item in listed["messages"]]
    assert roles == ["user", "agent"]
    agent = listed["messages"][1]
    assert set(agent) == {"id", "ts", "role", "text"}
    assert "GAME_API_KEY is not configured" in agent["text"]
    assert agent["ts"] > body["message"]["ts"]

    health = client.get("/health").json()
    assert health["game_api_key_set"] is False
    assert health["game_api_key_v2"] is False
    assert health["status"] == "ok"
    root = client.get("/").json()
    assert root["messages"] == 2
    assert root["game_api_key_set"] is False


def test_bad_prefix_stores_agent_error(client, monkeypatch):
    monkeypatch.setenv("GAME_API_KEY", "not-a-v2-key")
    sent = client.post("/send", json={"text": "BTC", "user": "Simzy"})
    assert sent.status_code == 200
    agent = client.get("/messages", params={"since": sent.json()["message"]["ts"]}).json()["messages"]
    assert len(agent) == 1
    assert agent[0]["role"] == "agent"
    assert "apt-" in agent[0]["text"]
    assert "not-a-v2-key" not in agent[0]["text"]
    health = client.get("/health").json()
    assert health["game_api_key_set"] is True
    assert health["game_api_key_v2"] is False
    assert "not-a-v2-key" not in json.dumps(health)


def test_chat_agent_reuses_session_and_persists_id(client, monkeypatch, tmp_path):
    monkeypatch.setenv("GAME_API_KEY", "apt-test-key-value")
    first = client.post("/send", json={"text": "First question", "user": "analysis-app"})
    second = client.post("/send", json={"text": "Follow up", "user": "analysis-app"})
    assert first.status_code == second.status_code == 200
    assert len(FakeAgent.created) == 1

    sessions = json.loads((tmp_path / "sessions.json").read_text())
    assert sessions["analysis-app"]["conversation_id"] == "conv-1"
    assert sessions["analysis-app"]["prompt_version"] == app.PROMPT_VERSION

    app._chats.clear()
    third = client.post("/send", json={"text": "After restart", "user": "analysis-app"})
    assert third.status_code == 200
    assert len(FakeAgent.created) == 1

    messages = client.get("/messages", params={"since": 0}).json()["messages"]
    agent_texts = [item["text"] for item in messages if item["role"] == "agent"]
    assert agent_texts[0].startswith("Lenses on First")
    assert "apt-test-key-value" not in json.dumps(messages)
    health = client.get("/").json()
    assert health["game_api_key_set"] is True
    assert health["game_api_key_v2"] is True
    assert "apt-test-key-value" not in json.dumps(health)


def test_finished_chat_starts_a_new_session(client, monkeypatch, tmp_path):
    monkeypatch.setenv("GAME_API_KEY", "apt-test-key-value")

    class DoneChat(FakeChat):
        def next(self, message):
            self.messages.append(message)
            return FakeResponse("done reply", is_finished=True)

    class DoneAgent(FakeAgent):
        def create_chat(self, partner_id, partner_name, action_space=None, get_state_fn=None):
            chat = DoneChat(f"conv-{len(FakeAgent.created) + 1}-done")
            FakeAgent.created.append((partner_id, partner_name, chat))
            return chat

    monkeypatch.setattr(app, "ChatAgent", DoneAgent)
    sent = client.post("/send", json={"text": "wrap up", "user": "Simzy"})
    assert sent.status_code == 200
    assert not (tmp_path / "sessions.json").exists() or "Simzy" not in json.loads(
        (tmp_path / "sessions.json").read_text()
    )
    client.post("/send", json={"text": "again", "user": "Simzy"})
    assert len(FakeAgent.created) == 2


def test_fresh_chat_failure_does_not_retry(client, monkeypatch):
    monkeypatch.setenv("GAME_API_KEY", "apt-test-key-value")

    class AlwaysFail(FakeChat):
        def next(self, message):
            raise RuntimeError("upstream down apt-test-key-value")

    class FailAgent(FakeAgent):
        def create_chat(self, partner_id, partner_name, action_space=None, get_state_fn=None):
            chat = AlwaysFail(f"conv-{len(FakeAgent.created) + 1}")
            FakeAgent.created.append((partner_id, partner_name, chat))
            return chat

    monkeypatch.setattr(app, "ChatAgent", FailAgent)
    sent = client.post("/send", json={"text": "hello", "user": "Simzy"})
    assert sent.status_code == 200
    assert len(FakeAgent.created) == 1
    agent = client.get("/messages", params={"since": sent.json()["message"]["ts"]}).json()["messages"][0]
    assert agent["role"] == "agent"
    assert "could not reach" in agent["text"]
    assert "apt-test-key-value" not in agent["text"]
    assert "apt-***" in agent["text"]


def test_stale_session_is_retried_once(client, monkeypatch):
    monkeypatch.setenv("GAME_API_KEY", "apt-test-key-value")
    first = client.post("/send", json={"text": "first", "user": "Simzy"})
    assert first.status_code == 200
    assert len(FakeAgent.created) == 1
    FakeAgent.created[0][2].fail_times = 1
    second = client.post("/send", json={"text": "second", "user": "Simzy"})
    assert second.status_code == 200
    assert len(FakeAgent.created) == 2
    agent_texts = [
        item["text"]
        for item in client.get("/messages", params={"since": 0}).json()["messages"]
        if item["role"] == "agent"
    ]
    assert any("second" in text for text in agent_texts)


def test_manual_respond_and_pending(client):
    denied = client.post("/respond", json={"text": "nope", "secret": "wrong"})
    assert denied.status_code == 401
    pending_denied = client.get("/pending", params={"secret": "wrong"})
    assert pending_denied.status_code == 401

    sent = client.post("/send", json={"text": "waiting", "user": "Simzy"})
    user_ts = sent.json()["message"]["ts"]
    # The missing-key path already stored an agent reply, so nothing is pending.
    assert client.get("/pending", params={"secret": "test-secret"}).json()["count"] == 0

    posted = client.post("/respond", json={"text": "manual note", "secret": "test-secret"})
    assert posted.status_code == 200
    assert posted.json()["message"]["role"] == "agent"
    assert set(posted.json()["message"]) == {"id", "ts", "role", "text"}

    recent = client.get("/messages", params={"since": user_ts}).json()["messages"]
    assert any(item["text"] == "manual note" for item in recent)

    empty = client.post("/send", json={"text": "   ", "user": "Simzy"})
    assert empty.status_code == 400
    assert empty.json()["error"] == "empty message"
    blank = client.post("/respond", json={"text": "  ", "secret": "test-secret"})
    assert blank.status_code == 400


def test_sdk_exports_chat_agent():
    from game_sdk.game.chat_agent import Chat as SdkChat
    from game_sdk.game.chat_agent import ChatAgent as SdkChatAgent

    assert SdkChatAgent is not None
    assert SdkChat is not None


def test_scrub_hides_dashboard_deferral_phrases():
    cleaned = app.scrub_for_dashboard(
        "Check Telegram. I'll review and get back to you. Based on its liquidity, I am on it."
    )
    lowered = cleaned.lower()
    assert "telegram" not in lowered
    assert "i'll review" not in lowered
    assert "get back to you" not in lowered
    assert "on it" not in lowered
    assert "on its" not in lowered
