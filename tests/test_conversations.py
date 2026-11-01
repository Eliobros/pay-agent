"""Persistent conversation history: storage, limits, expiry and isolation."""

import json
from types import SimpleNamespace

from payagent import config, conversations, db, stores
import app as app_module


def test_turns_are_persisted_with_roles_and_timestamps(database):
    conversation = conversations.open_conversation("store_demo", "visitor-1")
    conversations.append(conversation["id"], "store_demo", "user", "hello")
    conversations.append(conversation["id"], "store_demo", "assistant", "hi there")

    rows = conversations.messages_of(conversation["id"], "store_demo")
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["created_at"]
    assert conversations.load(conversation["id"], "store_demo") == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_tool_calls_and_order_references_survive_a_reload(database):
    conversation = conversations.open_conversation("store_demo", "visitor-1")
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "create_checkout", "arguments": '{"product_id": "p5"}'},
        }
    ]
    conversations.append(conversation["id"], "store_demo", "user", "buy the monitor")
    conversations.append(conversation["id"], "store_demo", "assistant", "", tool_calls=calls)
    conversations.append(
        conversation["id"],
        "store_demo",
        "tool",
        json.dumps({"order_id": "ORDER-1"}),
        tool_call_id="call_1",
        order_id="ORDER-1",
    )

    history = conversations.load(conversation["id"], "store_demo")
    assert history[1]["tool_calls"] == calls
    assert history[2] == {
        "role": "tool",
        "content": '{"order_id": "ORDER-1"}',
        "tool_call_id": "call_1",
    }
    rows = conversations.messages_of(conversation["id"], "store_demo")
    assert rows[2]["order_id"] == "ORDER-1"


def test_the_same_visitor_returns_to_the_same_conversation(database):
    first = conversations.open_conversation("store_demo", "visitor-1")
    second = conversations.open_conversation("store_demo", "visitor-1")
    assert first["id"] == second["id"]
    assert db.query("SELECT * FROM conversations") != []


def test_conversations_are_isolated_between_stores(database):
    other = stores.create_store("Other Store")
    mine = conversations.open_conversation("store_demo", "visitor-1")
    theirs = conversations.open_conversation(other["id"], "visitor-1")

    assert mine["id"] != theirs["id"]
    conversations.append(mine["id"], "store_demo", "user", "a private order")

    assert conversations.load(theirs["id"], other["id"]) == []
    assert conversations.messages_of(mine["id"], other["id"]) == []
    assert conversations.messages_of(theirs["id"], other["id"]) == []


def test_expired_conversations_start_fresh(database):
    conversation = conversations.open_conversation("store_demo", "visitor-1")
    conversations.append(conversation["id"], "store_demo", "user", "old context")
    db.execute(
        "UPDATE conversations SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (conversation["id"],),
    )

    reopened = conversations.open_conversation("store_demo", "visitor-1")
    assert reopened["id"] == conversation["id"]
    assert conversations.load(reopened["id"], "store_demo") == []
    assert db.query("SELECT * FROM messages") == []


def test_history_is_capped_at_max_history(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_HISTORY", 4)
    conversation = conversations.open_conversation("store_demo", "visitor-1")
    for i in range(6):
        conversations.append(conversation["id"], "store_demo", "user", f"question {i}")
        conversations.append(conversation["id"], "store_demo", "assistant", f"answer {i}")

    removed = conversations.trim(conversation["id"], "store_demo")
    assert removed > 0

    rows = conversations.messages_of(conversation["id"], "store_demo")
    assert len(rows) <= 4
    assert rows[0]["role"] == "user"
    assert rows[-1]["content"] == "answer 5"
    assert len(conversations.load(conversation["id"], "store_demo")) <= 4


def test_history_never_starts_with_an_orphan_tool_result(database):
    conversation = conversations.open_conversation("store_demo", "visitor-1")
    conversations.append(conversation["id"], "store_demo", "user", "q1")
    conversations.append(
        conversation["id"],
        "store_demo",
        "assistant",
        "calling",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "search_products", "arguments": "{}"}}],
    )
    conversations.append(conversation["id"], "store_demo", "tool", "{}", tool_call_id="c1")

    # A window that only holds the tool exchange is dropped entirely rather than
    # sending a tool result that references a trimmed assistant message.
    assert conversations.load(conversation["id"], "store_demo", limit=2) == []

    full = conversations.load(conversation["id"], "store_demo", limit=3)
    assert [message["role"] for message in full] == ["user", "assistant", "tool"]


def test_purge_removes_only_expired_conversations(database):
    expired = conversations.open_conversation("store_demo", "old-visitor")
    active = conversations.open_conversation("store_demo", "new-visitor")
    conversations.append(expired["id"], "store_demo", "user", "bye")
    db.execute(
        "UPDATE conversations SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (expired["id"],),
    )

    assert conversations.purge_expired() == 1
    assert db.query("SELECT * FROM messages") == []
    assert db.query_one("SELECT 1 FROM conversations WHERE id = ?", (active["id"],))


def test_purge_is_scoped_to_one_store(database):
    other = stores.create_store("Other Store")
    mine = conversations.open_conversation("store_demo", "visitor-1")
    theirs = conversations.open_conversation(other["id"], "visitor-1")
    db.execute("UPDATE conversations SET expires_at = '2000-01-01T00:00:00+00:00'")

    assert conversations.purge_expired("store_demo") == 1
    assert db.query_one("SELECT 1 FROM conversations WHERE id = ?", (theirs["id"],))
    assert mine["id"] != theirs["id"]


# ---------------------------------------------------------------- through the API
class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Response:
    def __init__(self, message):
        self.choices = [SimpleNamespace(message=message)]


class _FakeClient:
    """Records the messages the assistant would send to the model API."""

    def __init__(self, sent):
        self.sent = sent
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Copy: the caller keeps appending to its list after the response.
        self.sent.append([dict(message) for message in kwargs["messages"]])
        return _Response(_Message("Sure, here you go."))


def test_chat_resumes_the_stored_history(client, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "client", _FakeClient(sent))

    first = client.post("/chat", json={"message": "hi", "user_id": "visitor-1"})
    second = client.post("/chat", json={"message": "and now?", "user_id": "visitor-1"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    # Nothing is kept in memory: the second request rebuilt the context from SQLite.
    resumed = sent[1]
    assert resumed[0]["role"] == "system"
    contents = [message["content"] for message in resumed]
    assert "hi" in contents
    assert "Sure, here you go." in contents
    assert contents[-1] == "and now?"

    assert len(db.query("SELECT * FROM messages")) == 4


def test_chat_history_is_isolated_per_store(client, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "client", _FakeClient(sent))
    other = stores.create_store("Other Store")

    client.post("/chat", json={"message": "store demo question", "user_id": "visitor-1"})
    client.post(
        "/chat",
        json={"message": "other store question", "user_id": "visitor-1", "store_id": other["id"]},
    )

    assert "store demo question" not in [m["content"] for m in sent[1]]
