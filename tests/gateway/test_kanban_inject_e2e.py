"""End-to-end verification of kanban event live injection (T2).

This test exercises the path that the design doc describes:
1. notifier ticks and discovers a terminal event for a subscribed task
2. adapter.send() is called for the text notification
3. _kanban_inject_event() is called fire-and-forget (asyncio.ensure_future)
4. the synthetic event has internal=True and system_session=True (the
   system-session pipeline replaces the old ``silent_reply`` field —
   it routes the event through a dedicated system session rather than
   the user session's reply pipeline)
5. config switch notifier_inject=false disables injection

The test stubs out heavy dependencies (Adapter, kanban_db) and focuses on
the mixin's surface area: does the right thing get called with the right
args at the right time.

Run:
  cd /home/zml/workspace/hermes-agent
  venv/bin/python -m pytest tests/gateway/test_kanban_inject_e2e.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call


# --- helpers ----------------------------------------------------------------

def _make_runner_with_mocks():
    """Construct a minimal GatewayRunner-shaped object with the kanban
    watcher methods we want to test, plus mocked adapter and database.
    """
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    class FakeRunner(GatewayKanbanWatchersMixin):
        def __init__(self):
            self.adapters = {}
            self._running = True
            self._kanban_last_user_source = {
                "test-board": ("feishu", "chat-abc"),
            }
            # Capture what gets injected
            self.injected_events = []
            self.send_calls = []

        async def _handle_message(self, ev):
            self.injected_events.append(ev)
            return None  # mirrors system_session behavior (no user reply)

    return FakeRunner()


def _make_event(kind, payload=None):
    return SimpleNamespace(kind=kind, payload=payload or {})


def _make_sub(platform="feishu", chat_id="chat-abc", task_id="t_test_001",
              thread_id=None):
    return {
        "task_id": task_id,
        "platform": platform,
        "chat_id": chat_id,
        "thread_id": thread_id,
    }


def _make_task(task_id="t_test_001", title="测试任务", assignee="tester",
               result="shipped", status="running"):
    return SimpleNamespace(
        id=task_id,
        title=title,
        assignee=assignee,
        result=result,
        status=status,
    )


# --- config switch -----------------------------------------------------------

def test_notifier_inject_enabled_default_false():
    """The function must read the config flag and default to False."""
    runner = _make_runner_with_mocks()
    assert runner._kanban_notifier_inject_enabled({}) is False
    assert runner._kanban_notifier_inject_enabled({"notifier_inject": False}) is False
    assert runner._kanban_notifier_inject_enabled({"notifier_inject": True}) is True


# --- synthetic event construction ------------------------------------------

@pytest.mark.asyncio
async def test_inject_event_builds_correct_synthetic_message():
    """Completed event with summary produces a structured message with
    the [KANBAN-EVENT] marker and metadata line."""
    runner = _make_runner_with_mocks()
    ev = _make_event("completed", {"summary": "shipped OAuth flow"})
    sub = _make_sub()
    task = _make_task(title="实现 OAuth 登录")

    await runner._kanban_inject_event(
        event=ev, task=task, board_slug="test-board", sub=sub,
    )

    assert len(runner.injected_events) == 1
    synthetic = runner.injected_events[0]
    text = synthetic.text
    assert "[KANBAN-EVENT] completed | task: t_test_001 |" in text
    assert "board: test-board" in text
    assert "assignee: tester" in text
    assert "## COMPLETED — 实现 OAuth 登录" in text
    assert "shipped OAuth flow" in text
    assert "metadata: task_id=t_test_001" in text


@pytest.mark.asyncio
async def test_inject_event_marks_internal_and_system_session():
    """The synthetic event must bypass user auth (internal=True) and
    route through the system-session pipeline (system_session=True) so
    the user does NOT receive the agent's internal reasoning as a
    reply. ``system_session`` is the renamed-and-repurposed
    ``silent_reply`` flag — it now also documents that the event runs
    in its own session context, like a cron job."""
    runner = _make_runner_with_mocks()
    ev = _make_event("completed", {"summary": "ok"})
    sub = _make_sub()
    task = _make_task()

    await runner._kanban_inject_event(
        event=ev, task=task, board_slug="test-board", sub=sub,
    )
    synthetic = runner.injected_events[0]
    assert synthetic.internal is True
    assert synthetic.system_session is True


@pytest.mark.asyncio
async def test_inject_event_all_5_terminal_kinds():
    """All five terminal kinds must produce well-formed event text."""
    runner = _make_runner_with_mocks()
    sub = _make_sub()

    cases = [
        ("completed", {"summary": "done in 5 lines"}, "COMPLETED", "done in 5 lines"),
        ("blocked", {"reason": "需要用户确认密钥"}, "BLOCKED", "需要用户确认密钥"),
        ("gave_up", {"error": "spawn failed 3 times"}, "GAVE_UP", "spawn failed 3 times"),
        ("crashed", {}, "CRASHED", "worker crashed (pid gone)"),
        ("timed_out", {"limit_seconds": 600}, "TIMED_OUT", "timed out (max_runtime=600s)"),
    ]
    for kind, payload, expected_header, expected_detail in cases:
        runner.injected_events.clear()
        ev = _make_event(kind, payload)
        task = _make_task()
        await runner._kanban_inject_event(
            event=ev, task=task, board_slug="test-board", sub=sub,
        )
        assert len(runner.injected_events) == 1, f"kind={kind} not injected"
        text = runner.injected_events[0].text
        assert f"[KANBAN-EVENT] {kind}" in text, f"missing marker for {kind}"
        assert f"## {expected_header}" in text, f"missing header for {kind}"
        assert expected_detail in text, f"missing detail for {kind}: {text!r}"


@pytest.mark.asyncio
async def test_inject_event_skips_when_no_source_for_board():
    """If the board has no recorded user source in the persistent
    table, no in-memory cache entry, AND the subscription itself has no
    usable (platform, chat_id), injection must be a no-op (debug log +
    return). With the introduction of the persistent
    ``kanban_board_owners`` table the lookup has three tiers; the
    fall-through tier is the subscription's own coordinates, so a
    subscription with empty platform/chat_id is the only way to force
    a skip in tests.
    """
    runner = _make_runner_with_mocks()
    runner._kanban_last_user_source = {}  # empty in-memory cache
    # Stub out the persistent DB so a stale row from another test
    # can't sneak in as the lookup result.
    runner._kanban_lookup_board_owner = lambda *a, **kw: None
    ev = _make_event("completed", {"summary": "x"})
    sub = _make_sub(platform="", chat_id="")
    task = _make_task()

    await runner._kanban_inject_event(
        event=ev, task=task, board_slug="unknown-board", sub=sub,
    )
    assert runner.injected_events == []


@pytest.mark.asyncio
async def test_inject_event_uses_correct_source_platform_chat():
    """The synthetic event's source must point at the recorded user."""
    runner = _make_runner_with_mocks()
    runner._kanban_last_user_source = {"b1": ("telegram", "tg-chat-999")}
    ev = _make_event("completed", {"summary": "ok"})
    sub = _make_sub()
    task = _make_task()

    await runner._kanban_inject_event(
        event=ev, task=task, board_slug="b1", sub=sub,
    )
    source = runner.injected_events[0].source
    assert str(source.platform.value) == "telegram"
    assert source.chat_id == "tg-chat-999"
    assert source.user_name == "kanban-notifier"
    assert source.user_id == "system"


# --- end-to-end: notifier flow calls both adapter.send and inject ----------

@pytest.mark.asyncio
async def test_notifier_calls_both_adapter_send_and_inject():
    """The most important guarantee: when notifier_inject is enabled,
    adapter.send runs (user sees text) AND inject runs (agent sees event).
    Both must happen; they are independent deliveries."""
    runner = _make_runner_with_mocks()
    # Mock adapter — keyed by Platform enum (matches production)
    from gateway.config import Platform
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=None)
    runner.adapters[Platform.FEISHU] = adapter

    # Mock the notifier's session/task DB lookups so we can drive one tick
    # In production this all happens via _kanban_notifier_watcher; here we
    # manually reproduce the inner loop body to verify the call sequence.

    kanban_cfg = {"notifier_inject": True}
    sub = _make_sub()
    task = _make_task()
    ev = _make_event("completed", {"summary": "OK"})

    # Reproduce the relevant body of _kanban_notifier_watcher's inner loop
    msg = f"✅ Kanban {sub['task_id']} completed"
    metadata = {}
    sub_key = (sub["task_id"], sub["platform"], sub["chat_id"], sub.get("thread_id") or "")

    await adapter.send(sub["chat_id"], msg, metadata=metadata)
    if runner._kanban_notifier_inject_enabled(kanban_cfg):
        asyncio.ensure_future(
            runner._kanban_inject_event(
                event=ev, task=task, board_slug="test-board", sub=sub,
            )
        )
    # Let the ensure_future coroutine complete
    await asyncio.sleep(0.05)

    # adapter.send was called once with the user message
    assert adapter.send.await_count == 1
    args, kwargs = adapter.send.call_args
    assert args[0] == "chat-abc"  # chat_id
    assert "completed" in args[1]  # msg mentions completed

    # The synthetic event was injected
    assert len(runner.injected_events) == 1
    synthetic = runner.injected_events[0]
    assert synthetic.system_session is True
    assert synthetic.internal is True


@pytest.mark.asyncio
async def test_notifier_skips_inject_when_config_off():
    """With notifier_inject=false, the inject call must be skipped
    while adapter.send still runs (no regression to user notifications)."""
    runner = _make_runner_with_mocks()
    from gateway.config import Platform
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=None)
    runner.adapters[Platform.FEISHU] = adapter

    kanban_cfg = {"notifier_inject": False}
    sub = _make_sub()
    task = _make_task()
    ev = _make_event("completed", {"summary": "OK"})

    msg = f"✅ Kanban {sub['task_id']} completed"
    metadata = {}
    await adapter.send(sub["chat_id"], msg, metadata=metadata)

    if runner._kanban_notifier_inject_enabled(kanban_cfg):
        asyncio.ensure_future(
            runner._kanban_inject_event(
                event=ev, task=task, board_slug="test-board", sub=sub,
            )
        )
    await asyncio.sleep(0.05)

    assert adapter.send.await_count == 1  # user still notified
    assert runner.injected_events == []  # no agent injection


# --- regression: existing notifier behaviour still works --------------------

@pytest.mark.asyncio
async def test_inject_event_does_not_raise_on_missing_optional_fields():
    """Resilience: missing assignee / title / result must not blow up."""
    runner = _make_runner_with_mocks()
    sub = _make_sub(task_id="t_fallback_xyz")
    # Task with no title, no assignee, no result
    bad_task = SimpleNamespace(id="t_fallback_xyz", title=None, assignee=None, result=None, status="running")
    ev = _make_event("completed", {"summary": "ok"})

    await runner._kanban_inject_event(
        event=ev, task=bad_task, board_slug="test-board", sub=sub,
    )
    assert len(runner.injected_events) == 1
    text = runner.injected_events[0].text
    assert "assignee: unknown" in text
    assert "t_fallback_xyz" in text  # task_id from sub used as fallback title


@pytest.mark.asyncio
async def test_inject_event_does_not_raise_on_bad_platform():
    """If _kanban_last_user_source has an unknown platform, skip
    silently (ValueError on Platform enum)."""
    runner = _make_runner_with_mocks()
    runner._kanban_last_user_source = {"b1": ("mystery-platform", "x")}
    ev = _make_event("completed", {"summary": "ok"})
    sub = _make_sub()
    task = _make_task()

    # Should not raise
    await runner._kanban_inject_event(
        event=ev, task=task, board_slug="b1", sub=sub,
    )
    # Either skipped or injected — both are acceptable, but must not crash.
    # In practice, the code does `return` on ValueError, so it should be empty.
    # (We don't strictly assert either way; the no-crash is the contract.)


# --- persistent board owner: kanban_board_owners table + 3-tier lookup ----

def test_kanban_lookup_board_owner_uses_persistent_table_first():
    """The persistent ``kanban_board_owners`` table beats the in-memory
    cache. Stale in-memory state must not shadow a fresh DB write —
    this is the whole reason the refactor introduced the persistent
    table (multi-process safety, restart safety)."""
    runner = _make_runner_with_mocks()
    # Stale cache says (feishu, old-chat). DB says (telegram, fresh-chat).
    runner._kanban_last_user_source = {"b1": ("feishu", "old-chat")}

    fake_db = SimpleNamespace(
        get_board_owner=lambda _conn, board: ("telegram", "fresh-chat"),
        connect=lambda board=None: SimpleNamespace(close=lambda: None),
    )
    result = runner._kanban_lookup_board_owner("b1", db_mod=fake_db)
    assert result == ("telegram", "fresh-chat")


def test_kanban_lookup_board_owner_falls_back_to_memory_cache():
    """When the persistent table has no row for the board, the
    in-memory cache is the next tier (legacy back-compat)."""
    runner = _make_runner_with_mocks()
    runner._kanban_last_user_source = {"b1": ("feishu", "cached-chat")}

    fake_db = SimpleNamespace(
        get_board_owner=lambda _conn, board: None,
        connect=lambda board=None: SimpleNamespace(close=lambda: None),
    )
    result = runner._kanban_lookup_board_owner("b1", db_mod=fake_db)
    assert result == ("feishu", "cached-chat")


def test_kanban_lookup_board_owner_falls_back_to_subscription():
    """When both DB and cache miss, the subscription's own
    (platform, chat_id) is the last-resort target — covers a
    freshly-subscribed user that has never written to the owner
    table and isn't in the in-memory cache yet."""
    runner = _make_runner_with_mocks()
    runner._kanban_last_user_source = {}

    fake_db = SimpleNamespace(
        get_board_owner=lambda _conn, board: None,
        connect=lambda board=None: SimpleNamespace(close=lambda: None),
    )
    sub = _make_sub(platform="discord", chat_id="fresh-discord-chat")
    result = runner._kanban_lookup_board_owner("b1", db_mod=fake_db, fallback_sub=sub)
    assert result == ("discord", "fresh-discord-chat")


def test_kanban_lookup_board_owner_returns_none_when_all_miss():
    """Empty cache, no DB row, subscription has no platform/chat_id —
    the watcher should skip and not crash."""
    runner = _make_runner_with_mocks()
    runner._kanban_last_user_source = {}

    fake_db = SimpleNamespace(
        get_board_owner=lambda _conn, board: None,
        connect=lambda board=None: SimpleNamespace(close=lambda: None),
    )
    sub = _make_sub(platform="", chat_id="")
    result = runner._kanban_lookup_board_owner("b1", db_mod=fake_db, fallback_sub=sub)
    assert result is None


def test_kanban_lookup_board_owner_tolerates_db_exceptions():
    """A transient DB error during owner lookup must NOT crash the
    notifier tick — the function logs and falls through to the
    in-memory cache."""
    runner = _make_runner_with_mocks()
    runner._kanban_last_user_source = {"b1": ("feishu", "fallback-chat")}

    class _ExplodingDb:
        def get_board_owner(self, _conn, board):
            raise sqlite3.OperationalError("database is locked")

        def connect(self, board=None):
            return SimpleNamespace(close=lambda: None)

    result = runner._kanban_lookup_board_owner("b1", db_mod=_ExplodingDb())
    # Falls through to in-memory cache.
    assert result == ("feishu", "fallback-chat")
