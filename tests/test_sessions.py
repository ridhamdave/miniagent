"""
Tests for miniagent/sessions/store.py (SessionStore).

Strategy:
- Use tmp_path to redirect sessions_dir so tests never touch ~/.miniagent/sessions.
- Use monkeypatch to redirect get_config().sessions_dir to tmp_path.
- All tests are async (asyncio_mode = "auto" in pyproject.toml).
"""

import json
from pathlib import Path

import pytest

from miniagent.sessions import SessionStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(tmp_path: Path) -> SessionStore:
    """Return a SessionStore backed by tmp_path — no real ~/.miniagent writes."""
    return SessionStore(sessions_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# File creation
# ---------------------------------------------------------------------------

async def test_append_creates_file_if_not_exists(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entry = {"role": "user", "content": "Hello"}
    await store.append("session1", entry)

    session_file = tmp_path / "session1.jsonl"
    assert session_file.exists(), "session file should be created by append()"


async def test_append_creates_sessions_dir_if_not_exists(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "sessions"
    store = SessionStore(sessions_dir=str(nested))
    entry = {"role": "user", "content": "Hello"}
    await store.append("s1", entry)

    assert nested.exists(), "sessions_dir should be created on first write"
    assert (nested / "s1.jsonl").exists()


# ---------------------------------------------------------------------------
# Multiple entries / separate lines
# ---------------------------------------------------------------------------

async def test_append_multiple_entries_on_separate_lines(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello there!"},
        {"role": "user", "content": "How are you?"},
    ]
    for entry in entries:
        await store.append("conv", entry)

    session_file = tmp_path / "conv.jsonl"
    lines = [ln for ln in session_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 3, "each append should produce exactly one non-empty line"


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------

async def test_load_returns_entries_in_order(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [
        {"role": "user", "content": "msg1"},
        {"role": "assistant", "content": "msg2"},
        {"role": "user", "content": "msg3"},
    ]
    for e in entries:
        await store.append("order_test", e)

    loaded = await store.load("order_test")
    assert loaded == entries, "entries should be returned in append order"


async def test_load_limit_returns_last_n_entries(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [{"seq": i} for i in range(10)]
    for e in entries:
        await store.append("limit_test", e)

    loaded = await store.load("limit_test", limit=3)
    assert len(loaded) == 3
    assert loaded == entries[-3:], "load(limit=3) should return the last 3 entries"


async def test_load_limit_larger_than_file_returns_all(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [{"i": i} for i in range(5)]
    for e in entries:
        await store.append("large_limit", e)

    loaded = await store.load("large_limit", limit=100)
    assert loaded == entries


async def test_load_nonexistent_session_returns_empty_list(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    result = await store.load("does_not_exist")
    assert result == [], "loading a nonexistent session should return []"


async def test_load_default_limit_is_50(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [{"n": i} for i in range(60)]
    for e in entries:
        await store.append("big_session", e)

    loaded = await store.load("big_session")
    assert len(loaded) == 50
    assert loaded == entries[-50:]


# ---------------------------------------------------------------------------
# clear()
# ---------------------------------------------------------------------------

async def test_clear_deletes_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.append("to_clear", {"role": "user", "content": "bye"})
    session_file = tmp_path / "to_clear.jsonl"
    assert session_file.exists()

    await store.clear("to_clear")
    assert not session_file.exists(), "clear() should delete the session file"


async def test_clear_nonexistent_session_does_not_raise(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    # Must not raise — file never existed
    await store.clear("phantom_session")


async def test_clear_then_load_returns_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    await store.append("ephemeral", {"x": 1})
    await store.clear("ephemeral")
    result = await store.load("ephemeral")
    assert result == []


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------

async def test_json_roundtrip_str_int_list_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entry = {
        "id": "msg_abc123",
        "role": "user",
        "content": "Hello, world!",
        "count": 42,
        "tags": ["python", "asyncio", "jsonl"],
        "nested": {"key": "value", "num": 7},
    }
    await store.append("roundtrip", entry)
    loaded = await store.load("roundtrip")

    assert len(loaded) == 1
    assert loaded[0] == entry, "entry should survive a JSON round-trip unchanged"


async def test_json_roundtrip_multiple_mixed_entries(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    entries = [
        {"id": "a", "role": "user", "content": "hi", "tokens": 2, "flags": [True, False]},
        {"id": "b", "role": "assistant", "content": "hello", "score": 0.95, "refs": []},
        {"id": "c", "role": "user", "content": "bye", "meta": {"k": "v"}},
    ]
    for e in entries:
        await store.append("multi_round", e)

    loaded = await store.load("multi_round")
    assert loaded == entries


# ---------------------------------------------------------------------------
# Session key sanitization
# ---------------------------------------------------------------------------

async def test_session_key_sanitization(tmp_path: Path) -> None:
    """Special characters in session keys should be stripped, not cause errors."""
    store = make_store(tmp_path)
    await store.append("session/key!@#$%", {"data": 1})
    # Only alphanumeric and -_ survive sanitization
    expected_file = tmp_path / "sessionkey.jsonl"
    assert expected_file.exists()
    loaded = await store.load("session/key!@#$%")
    assert loaded == [{"data": 1}]


# ---------------------------------------------------------------------------
# monkeypatch integration: get_config().sessions_dir redirect
# ---------------------------------------------------------------------------

async def test_get_config_sessions_dir_is_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    SessionStore() with no arguments reads sessions_dir from get_config().
    Patch get_config to return a config pointing to tmp_path.
    """
    import miniagent.sessions.store as store_module
    from miniagent.config.types import MiniAgentConfig

    fake_config = MiniAgentConfig(sessions_dir=str(tmp_path))
    monkeypatch.setattr(store_module, "get_config", lambda: fake_config)

    store = SessionStore()  # no explicit sessions_dir — reads from patched get_config
    await store.append("cfg_test", {"hello": "world"})
    loaded = await store.load("cfg_test")
    assert loaded == [{"hello": "world"}]
    assert store.sessions_dir == tmp_path
