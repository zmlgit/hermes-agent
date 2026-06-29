"""Tests for the kanban-workflow plugin (v3.0)."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

def _load_plugin(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    from hermes_cli.plugins import get_plugin_manager
    mgr = get_plugin_manager()
    mgr.discover_and_load()
    return mgr, mgr._plugins.get("kanban-workflow")

def test_plugin_registers_hooks(monkeypatch, tmp_path):
    _mgr, loaded = _load_plugin(monkeypatch, tmp_path)
    assert loaded and loaded.enabled
    assert "subagent_start" in loaded.hooks_registered

def test_pre_tool_call_latches_board(monkeypatch, tmp_path):
    _mgr, loaded = _load_plugin(monkeypatch, tmp_path)
    from hermes_cli.kanban_db import _CURRENT_BOARD_OVERRIDE
    token = _CURRENT_BOARD_OVERRIDE.set(None)
    try:
        loaded.module._on_pre_tool_call(tool_name="kanban_show", args={"board": "prj-a"}, session_id="s1")
        assert _CURRENT_BOARD_OVERRIDE.get() == "prj-a"
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)

def test_subagent_start_propagates_board(monkeypatch, tmp_path):
    _mgr, loaded = _load_plugin(monkeypatch, tmp_path)
    from hermes_cli.kanban_db import _CURRENT_BOARD_OVERRIDE
    token = _CURRENT_BOARD_OVERRIDE.set("parent-board")
    try:
        loaded.module._on_subagent_start(parent_session_id="p1", child_session_id="c1")
        # Now child calls a tool without explicit board
        _CURRENT_BOARD_OVERRIDE.set(None)
        loaded.module._on_pre_tool_call(tool_name="kanban_show", args={}, session_id="c1")
        assert _CURRENT_BOARD_OVERRIDE.get() == "parent-board"
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)

def test_task_completed_hooks(monkeypatch, tmp_path):
    _mgr, loaded = _load_plugin(monkeypatch, tmp_path)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="P", assignee="lead", initial_status="running")
        # Force parent to running
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (parent,))
        child = kb.create_task(conn, title="C", assignee="coder", parents=[parent])
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        
        loaded.module._on_pre_tool_call("kanban_complete", {}, session_id="s1")
        kb.complete_task(conn, child, summary="done")
        
        # Verify auto complete parent
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (parent,)).fetchone()["status"] == "done"
    finally:
        conn.close()

def test_custom_config_loads(monkeypatch, tmp_path):
    _mgr, loaded = _load_plugin(monkeypatch, tmp_path)
    # Just ensure it doesn't crash and falls back to defaults when no config present
    assert "coder" in loaded.module._get_assignees()
    assert "typo" in loaded.module._get_trivial_keywords()
