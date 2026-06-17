"""Tests for M1: Core Loop MVP — verification + feedback + spiral convergence.

Covers all 5 subtasks:
  M1-1: parse_verification_config — YAML verification block parsing
  M1-2: _run_auto_verification — subprocess execution of verification commands
  M1-3: build_worker_context — structured feedback injection on verify-fail
  M1-4: reopen_for_remediation — task → ready for re-dispatch
  M1-5: count_verification_loops + loop-limit → blocked

Run:
  cd /home/zml/workspace/hermes-agent
  venv/bin/python -m pytest tests/hermes_cli/test_kanban_loop_m1.py -v
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# M1-1: parse_verification_config
# ---------------------------------------------------------------------------

class TestParseVerificationConfig:
    """M1-1: task body verification field parsing."""

    def test_parse_fenced_auto(self):
        """Fenced YAML ``verification.auto`` block is parsed correctly."""
        body = """## Task
Do the thing.

```yaml
verification:
  auto:
    - cmd: "pytest tests/ -x -q"
      expect: "exit_code == 0"
```
"""
        result = kb.parse_verification_config(body)
        assert result is not None
        assert "auto" in result
        assert len(result["auto"]) == 1
        assert result["auto"][0]["cmd"] == "pytest tests/ -x -q"
        assert result["auto"][0]["expect"] == "exit_code == 0"

    def test_parse_unfenced_auto(self):
        """Unfenced YAML ``verification.auto`` block is parsed correctly."""
        body = """## Task
Do the thing.

verification:
  auto:
    - cmd: "ruff check ."
      expect: "exit_code == 0"
"""
        result = kb.parse_verification_config(body)
        assert result is not None
        assert "auto" in result
        assert result["auto"][0]["cmd"] == "ruff check ."

    def test_parse_manual(self):
        """``verification.manual: true`` is parsed correctly."""
        body = """## Task
verification:
  manual: true
"""
        result = kb.parse_verification_config(body)
        assert result is not None
        assert result.get("manual") is True

    def test_parse_none_when_absent(self):
        """No verification block → None (backward-compatible default)."""
        body = "## Task\nJust do the thing."
        result = kb.parse_verification_config(body)
        assert result is None

    def test_parse_none_for_empty_body(self):
        """Empty/None body → None."""
        assert kb.parse_verification_config(None) is None
        assert kb.parse_verification_config("") is None
        assert kb.parse_verification_config("   ") is None

    def test_parse_multiple_auto_cmds(self):
        """Multiple auto commands are parsed as a list."""
        body = """```yaml
verification:
  auto:
    - cmd: "pytest tests/"
      expect: "exit_code == 0"
    - cmd: "ruff check ."
      expect: "exit_code == 0"
    - cmd: "mypy src/"
      expect: "exit_code == 0"
```
"""
        result = kb.parse_verification_config(body)
        assert result is not None
        assert len(result["auto"]) == 3


# ---------------------------------------------------------------------------
# M1-2: _run_auto_verification (tools layer)
# ---------------------------------------------------------------------------

class TestRunAutoVerification:
    """M1-2: auto-verification command execution."""

    def test_passing_command(self, tmp_path):
        """A command that exits 0 → passed=True."""
        from tools.kanban_tools import _run_auto_verification
        results = _run_auto_verification(str(tmp_path), [
            {"cmd": "true", "expect": "exit_code == 0"},
        ])
        assert len(results) == 1
        assert results[0]["passed"] is True
        assert results[0]["exit_code"] == 0

    def test_failing_command(self, tmp_path):
        """A command that exits non-zero → passed=False."""
        from tools.kanban_tools import _run_auto_verification
        results = _run_auto_verification(str(tmp_path), [
            {"cmd": "false", "expect": "exit_code == 0"},
        ])
        assert len(results) == 1
        assert results[0]["passed"] is False
        assert results[0]["exit_code"] != 0

    def test_stdout_captured(self, tmp_path):
        """stdout is captured and returned."""
        from tools.kanban_tools import _run_auto_verification
        results = _run_auto_verification(str(tmp_path), [
            {"cmd": "echo hello_world"},
        ])
        assert "hello_world" in results[0]["stdout"]

    def test_stderr_captured(self, tmp_path):
        """stderr is captured and returned."""
        from tools.kanban_tools import _run_auto_verification
        results = _run_auto_verification(str(tmp_path), [
            {"cmd": "echo err_msg >&2; exit 1"},
        ])
        assert "err_msg" in results[0]["stderr"]
        assert results[0]["passed"] is False

    def test_multiple_commands_mixed(self, tmp_path):
        """Multiple commands with mixed pass/fail."""
        from tools.kanban_tools import _run_auto_verification
        results = _run_auto_verification(str(tmp_path), [
            {"cmd": "true"},
            {"cmd": "false"},
            {"cmd": "true"},
        ])
        assert len(results) == 3
        assert results[0]["passed"] is True
        assert results[1]["passed"] is False
        assert results[2]["passed"] is True

    def test_empty_cmd_skipped(self, tmp_path):
        """Empty cmd strings are skipped."""
        from tools.kanban_tools import _run_auto_verification
        results = _run_auto_verification(str(tmp_path), [
            {"cmd": ""},
            {"cmd": "true"},
        ])
        assert len(results) == 1
        assert results[0]["passed"] is True

    def test_nonexistent_workspace(self):
        """Nonexistent workspace → cwd=None, commands still run."""
        from tools.kanban_tools import _run_auto_verification
        results = _run_auto_verification("/nonexistent/path", [
            {"cmd": "true"},
        ])
        assert len(results) == 1
        assert results[0]["passed"] is True


# ---------------------------------------------------------------------------
# M1-3: verification_history + build_worker_context feedback injection
# ---------------------------------------------------------------------------

class TestVerificationFeedback:
    """M1-3: structured feedback injection into worker_context."""

    def test_verification_history_empty(self, kanban_home):
        """No verification events → empty list."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            assert kb.verification_history(conn, tid) == []

    def test_verification_history_after_events(self, kanban_home):
        """verification_history returns events in chronological order."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "false", "exit_code": 1}],
                    "loop_depth": 0,
                })
                kb._append_event(conn, tid, "feedback_provided", {
                    "failures": [{"cmd": "false"}],
                })
                kb._append_event(conn, tid, "verified", {"auto": True})

            hist = kb.verification_history(conn, tid)
            assert len(hist) == 3
            assert hist[0]["kind"] == "verification_failed"
            assert hist[1]["kind"] == "feedback_provided"
            assert hist[2]["kind"] == "verified"

    def test_build_worker_context_includes_feedback(self, kanban_home):
        """worker_context includes verification feedback on prior failure."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test task", assignee="w")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{
                        "cmd": "pytest tests/",
                        "exit_code": 1,
                        "stderr": "2 failed, 3 passed",
                        "stdout": "",
                        "passed": False,
                    }],
                    "previous_attempt": "tried to fix bug X",
                    "loop_depth": 0,
                })

            ctx = kb.build_worker_context(conn, tid)
            assert "Verification Feedback" in ctx
            assert "pytest tests/" in ctx
            assert "2 failed, 3 passed" in ctx
            assert "loop **0**" in ctx
            assert "tried to fix bug X" in ctx

    def test_build_worker_context_no_feedback_when_clean(self, kanban_home):
        """No verification_failed events → no feedback section."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="clean task", assignee="w")
            ctx = kb.build_worker_context(conn, tid)
            assert "Verification Feedback" not in ctx


# ---------------------------------------------------------------------------
# M1-4: reopen_for_remediation
# ---------------------------------------------------------------------------

class TestReopenForRemediation:
    """M1-4: spiral convergence — verification fail → reopen."""

    def test_reopen_transitions_to_ready(self, kanban_home):
        """reopen_for_remediation transitions running → ready."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            claimed = kb.claim_task(conn, tid, claimer="test")
            assert claimed is not None
            assert kb.get_task(conn, tid).status == "running"

            ok = kb.reopen_for_remediation(
                conn, tid,
                loop_depth=0,
                failures=[{"cmd": "false", "exit_code": 1}],
            )
            assert ok is True
            t = kb.get_task(conn, tid)
            assert t.status == "ready"

    def test_reopen_writes_remediation_event(self, kanban_home):
        """reopen_for_remediation writes a remediation_created event."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            kb.claim_task(conn, tid, claimer="test")

            kb.reopen_for_remediation(
                conn, tid,
                loop_depth=1,
                failures=[{"cmd": "false"}],
                feedback={"failures": [{"cmd": "false"}]},
            )

            events = kb.list_events(conn, tid)
            kinds = [e.kind for e in events]
            assert "remediation_created" in kinds

    def test_reopen_ends_run_as_rejected(self, kanban_home):
        """The run is closed with outcome='rejected'."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            kb.claim_task(conn, tid, claimer="test")

            kb.reopen_for_remediation(
                conn, tid,
                loop_depth=0,
                failures=[{"cmd": "false"}],
            )

            runs = kb.list_runs(conn, tid)
            closed = [r for r in runs if r.ended_at is not None]
            assert any(r.outcome == "rejected" for r in closed)


# ---------------------------------------------------------------------------
# M1-5: count_verification_loops + loop-limit
# ---------------------------------------------------------------------------

class TestLoopDepthAndLimit:
    """M1-5: loop-depth tracking and upper-limit → blocked."""

    def test_count_zero_initially(self, kanban_home):
        """No prior failures → loop_depth=0."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            assert kb.count_verification_loops(conn, tid) == 0

    def test_count_increments(self, kanban_home):
        """Each verification_failed event increments the count."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {"loop_depth": 0})
                kb._append_event(conn, tid, "verification_failed", {"loop_depth": 1})
                kb._append_event(conn, tid, "verification_failed", {"loop_depth": 2})
            assert kb.count_verification_loops(conn, tid) == 3

    def test_loop_limit_blocks(self, kanban_home, monkeypatch):
        """At max_loop_depth, verification failure → blocked (not reopened)."""
        monkeypatch.setenv("HERMES_KANBAN_MAX_LOOP_DEPTH", "2")
        from tools.kanban_tools import _handle_verification_failure
        from tools.registry import tool_error

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            kb.claim_task(conn, tid, claimer="test")

            # Pre-seed 2 verification_failed events so loop_depth=2.
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {"loop_depth": 0})
                kb._append_event(conn, tid, "verification_failed", {"loop_depth": 1})
            assert kb.count_verification_loops(conn, tid) == 2

            result = _handle_verification_failure(
                kb, conn, tid,
                kb.get_task(conn, tid),
                [{"cmd": "false", "exit_code": 1, "stderr": "fail", "stdout": "", "passed": False}],
                summary="attempt 3",
                expected_run_id=kb._current_run_id(conn, tid),
            )

            t = kb.get_task(conn, tid)
            assert t.status == "blocked"
            assert "error" in result or "FAILED" in result

    def test_loop_below_limit_reopens(self, kanban_home, monkeypatch):
        """Below max_loop_depth, verification failure → reopened (ready)."""
        monkeypatch.setenv("HERMES_KANBAN_MAX_LOOP_DEPTH", "3")
        from tools.kanban_tools import _handle_verification_failure

        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            kb.claim_task(conn, tid, claimer="test")

            # No prior failures → loop_depth=0, which is < 3.
            result = _handle_verification_failure(
                kb, conn, tid,
                kb.get_task(conn, tid),
                [{"cmd": "false", "exit_code": 1, "stderr": "fail", "stdout": "", "passed": False}],
                summary="attempt 1",
                expected_run_id=kb._current_run_id(conn, tid),
            )

            t = kb.get_task(conn, tid)
            assert t.status == "ready"
            assert "FAILED" in result

    def test_loop_depth_tracked_in_events(self, kanban_home):
        """verification_failed events carry loop_depth in payload."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            kb.claim_task(conn, tid, claimer="test")

            from tools.kanban_tools import _handle_verification_failure
            _handle_verification_failure(
                kb, conn, tid,
                kb.get_task(conn, tid),
                [{"cmd": "false", "exit_code": 1, "stderr": "", "stdout": "", "passed": False}],
                summary="attempt 1",
                expected_run_id=kb._current_run_id(conn, tid),
            )

            hist = kb.verification_history(conn, tid)
            vfails = [h for h in hist if h["kind"] == "verification_failed"]
            assert len(vfails) == 1
            assert "loop_depth" in vfails[0]["payload"]
            assert vfails[0]["payload"]["loop_depth"] == 0


# ---------------------------------------------------------------------------
# Integration: backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    """Tasks without verification.auto behave exactly as before."""

    def test_complete_without_verification(self, kanban_home):
        """Task with no verification block completes normally."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="plain", assignee="w")
            kb.claim_task(conn, tid, claimer="test")
            ok = kb.complete_task(conn, tid, summary="done")
            assert ok is True
            assert kb.get_task(conn, tid).status == "done"

    def test_complete_with_manual_verification(self, kanban_home):
        """Task with verification.manual=true completes normally (no auto gate)."""
        with kb.connect() as conn:
            body = "## Task\nverification:\n  manual: true\n"
            tid = kb.create_task(conn, title="manual", assignee="w", body=body)
            kb.claim_task(conn, tid, claimer="test")
            ok = kb.complete_task(conn, tid, summary="done")
            assert ok is True
            assert kb.get_task(conn, tid).status == "done"

    def test_no_verified_event_without_verification(self, kanban_home):
        """No ``verified`` event when task has no verification.auto."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="plain", assignee="w")
            kb.claim_task(conn, tid, claimer="test")
            kb.complete_task(conn, tid, summary="done")
            events = kb.list_events(conn, tid)
            kinds = [e.kind for e in events]
            assert "verified" not in kinds
            assert "verification_failed" not in kinds
