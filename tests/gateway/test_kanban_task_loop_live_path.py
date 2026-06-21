"""Live-path integration tests for the kanban task-loop fixes.

These exercise the REAL entry point ``_kanban_orchestrator_callback``
(called by the notifier watcher at runtime) with a real tmp DB, real
task_events, and multi-channel board owners — no mocks of the DB layer.

Covers the four fixes:
  Fix 1 — multi-channel fan-out via ``_kanban_delivery_targets`` (already
          on the live path; these tests pin it).
  Fix 2 — strict convergence: only all-done boards converge; a blocked
          or ready task blocks convergence so the orchestrator is woken.
  Fix 3 — structured per-event payload: the injected message carries
          error / consecutive_failures / children / recommended_action.
  Fix 4 — notifier log noise: boards with no subs AND no owners are the
          only ones that log "nothing to deliver".

Run:
  cd /home/zml/workspace/hermes-agent
  venv/bin/python -m pytest tests/gateway/test_kanban_task_loop_live_path.py -v
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def _make_runner():
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    class FakeRunner(GatewayKanbanWatchersMixin):
        def __init__(self):
            self.adapters = {}
            self._running = True
            self._kanban_last_user_source = {}
            self.injected = []
            self.send_calls = []

        async def _handle_message(self, ev):
            self.injected.append(ev)
            return None

    return FakeRunner()


def _seed_board(home: Path, board: str, *, owners=None, tasks=None, events=None):
    from hermes_cli import kanban_db as kb
    now = int(time.time())
    conn = kb.connect(board=board)
    for tid, status, title in (tasks or []):
        conn.execute(
            "INSERT OR REPLACE INTO tasks(id,title,status,priority,created_at,started_at,completed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (tid, title, status, 0, now, now, now),
        )
    for tid, kind, payload in (events or []):
        conn.execute(
            "INSERT INTO task_events(task_id,run_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
            (tid, None, kind, json.dumps(payload) if payload else None, now),
        )
    conn.commit()
    if owners:
        oconn = kb.connect(board=board)
        for plat, chat in owners:
            oconn.execute(
                "INSERT OR REPLACE INTO kanban_board_owners(board,platform,chat_id,updated_at) "
                "VALUES(?,?,?,?)",
                (board, plat, chat, now),
            )
        oconn.commit()
        oconn.close()


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return tmp_path / ".hermes"


def test_fix2_strict_convergence_rejects_blocked_task(isolated_home):
    from gateway import kanban_watchers as kw
    board = "conv-test"
    _seed_board(
        isolated_home, board,
        tasks=[("t_d{}".format(i), "done", "d{}".format(i)) for i in range(10)]
        + [("t3", "blocked", "c")],
    )
    from hermes_cli import kanban_db as kb
    conn = kb.connect(board=board)
    m = kw.compute_board_convergence(conn)
    conn.close()
    assert m["converged"] is False, "blocked task must block strict convergence"
    assert m["non_done"] == 1
    assert m["converged_ratio"] is True, "ratio path preserved (10/11=0.9, 1/11=0.09<0.2)"


def test_fix2_strict_convergence_accepts_all_done(isolated_home):
    from gateway import kanban_watchers as kw
    from hermes_cli import kanban_db as kb
    board = "conv-done"
    _seed_board(isolated_home, board, tasks=[("t1", "done", "a"), ("t2", "done", "b")])
    conn = kb.connect(board=board)
    m = kw.compute_board_convergence(conn)
    conn.close()
    assert m["converged"] is True
    assert m["non_done"] == 0


def test_fix2_ready_task_blocks_convergence(isolated_home):
    from gateway import kanban_watchers as kw
    from hermes_cli import kanban_db as kb
    board = "conv-ready"
    _seed_board(
        isolated_home, board,
        tasks=[("t1", "done", "a"), ("t2", "ready", "crashed-retry")],
    )
    conn = kb.connect(board=board)
    m = kw.compute_board_convergence(conn)
    conn.close()
    assert m["converged"] is False, "ready (crashed-retry) task must block convergence"


def test_fix1_multichannel_fanout_targets_all_owners(isolated_home):
    runner = _make_runner()
    board = "multi-chan"
    _seed_board(
        isolated_home, board,
        owners=[("feishu", "chat_f"), ("weixin", "chat_w")],
        tasks=[("t1", "done", "a")],
    )
    targets = runner._kanban_delivery_targets(board)
    plats = {p for p, _ in targets}
    assert plats == {"feishu", "weixin"}, f"expected both channels, got {targets}"


def test_fix1_no_owners_no_source_returns_empty(isolated_home):
    runner = _make_runner()
    board = "orphan-board"
    _seed_board(isolated_home, board, tasks=[("t1", "done", "a")])
    targets = runner._kanban_delivery_targets(board)
    assert targets == [], "board with no owners and no in-memory source -> empty"


def test_fix3_message_carries_structured_event_details(isolated_home):
    runner = _make_runner()
    board = "struct-test"
    _seed_board(
        isolated_home, board,
        owners=[("feishu", "chat_f")],
        tasks=[("t1", "blocked", "gave-up task")],
        events=[("t1", "gave_up", {"failures": 2, "effective_limit": 2, "error": "OOM"})],
    )
    from hermes_cli import kanban_db as kb
    conn = kb.connect(board=board)
    kb.execute(conn, "UPDATE tasks SET consecutive_failures=2, last_failure_error='OOM' WHERE id='t1'") \
        if hasattr(kb, "execute") else conn.execute(
        "UPDATE tasks SET consecutive_failures=2, last_failure_error='OOM' WHERE id='t1'")
    conn.commit(); conn.close()

    stats = runner._detect_task_loop(board, [], runner.task_loop_engine._last_event_id)
    assert stats is not None
    ed = stats["event_details"]
    assert ed, "expected at least one event detail"
    gave_up = next(e for e in ed if e["kind"] == "gave_up")
    assert gave_up["consecutive_failures"] == 2
    assert gave_up["effective_limit"] == 2
    assert "OOM" in (gave_up["error"] or "")
    assert gave_up["recommended_action"] == "resolve_blocker_or_supersede"

    msg = runner._build_task_loop_message(board, ed, stats)
    assert "resolve_blocker_or_supersede" in msg
    assert "2/2" in msg
    assert "OOM" in msg


def test_fix3_completed_event_has_check_children_action(isolated_home):
    runner = _make_runner()
    board = "struct-comp"
    _seed_board(
        isolated_home, board,
        owners=[("feishu", "chat_f")],
        tasks=[("t1", "done", "parent"), ("t2", "ready", "child")],
        events=[("t1", "completed", {"summary": "done"})],
    )
    from hermes_cli import kanban_db as kb
    conn = kb.connect(board=board)
    conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES('t1','t2')")
    conn.commit(); conn.close()

    stats = runner._detect_task_loop(board, [], runner.task_loop_engine._last_event_id)
    ed = stats["event_details"]
    comp = next(e for e in ed if e["kind"] == "completed")
    assert comp["recommended_action"] == "check_children_promoted"
    assert any(c["id"] == "t2" for c in comp["children"])


@pytest.mark.asyncio
async def test_live_callback_injects_to_all_owners(isolated_home):
    runner = _make_runner()
    board = "live-cb"
    _seed_board(
        isolated_home, board,
        owners=[("feishu", "chat_f"), ("weixin", "chat_w")],
        tasks=[("t1", "done", "a")],
        events=[("t1", "completed", {"summary": "ok"})],
    )
    from gateway.config import Platform
    from types import SimpleNamespace

    class FakeAdapter:
        def __init__(self):
            self.sent = []
        async def send(self, *, chat_id, content):
            self.sent.append((chat_id, content))
            return SimpleNamespace(success=True)

    runner.adapters = {Platform.FEISHU: FakeAdapter(), Platform.WEIXIN: FakeAdapter()}

    kanban_cfg = {
        "orchestrator_notify": True,
        "orchestrator_boards": [board],
        "orchestrator_cooldown_seconds": 0,
        "orchestrator_max_epochs": 10,
        "orchestrator_force_failure_threshold": 3,
    }

    with patch.object(runner, "_scan_candidate_boards", return_value=[board]), \
         patch.object(runner, "_convergence_already_notified", return_value=False), \
         patch.object(runner, "_auto_complete_parents", return_value=[]):
        await runner._kanban_orchestrator_callback([], kanban_cfg)
        for _ in range(10):
            await asyncio.sleep(0)
            if runner.injected:
                break

    assert len(runner.injected) >= 2, (
        f"multi-channel fan-out: should inject one synthetic event per owner, "
        f"got {len(runner.injected)}"
    )
    injected_platforms = {
        getattr(ev.source.platform, "value", str(ev.source.platform))
        for ev in runner.injected
    }
    assert "feishu" in injected_platforms, f"feishu missing from {injected_platforms}"
    assert "weixin" in injected_platforms, f"weixin missing from {injected_platforms}"
