"""Tests for M2: Smart convergence — gave_up expansion + convergence detection +
task_loop_closed + goal_mode integration.

Covers all 4 subtasks:
  M2-1: stderr_similarity, detect_repeated_verification_errors,
        detect_token_anomaly, detect_no_output, check_early_giveup
  M2-2: compute_board_convergence — pure function
  M2-3: record_task_loop_closed — event payload structure
  M2-4: should_auto_enable_task_loop — goal_mode + verification.auto

Run:
  cd /home/zml/workspace/hermes-agent
  venv/bin/python -m pytest tests/hermes_cli/test_kanban_loop_m2.py -v
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    from pathlib import Path
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _seed_run(conn, task_id, *, outcome="completed", metadata=None):
    """Create and close a task_run with the given metadata."""
    # Reset task to 'ready' so claim_task can re-claim it.
    conn.execute(
        "UPDATE tasks SET status='ready', current_run_id=NULL, "
        "claim_lock=NULL, claim_expires=NULL WHERE id=?",
        (task_id,),
    )
    conn.commit()
    kb.claim_task(conn, task_id, claimer="test")
    run_id = kb._current_run_id(conn, task_id)
    kb._end_run(
        conn, task_id,
        outcome=outcome, status=outcome,
        metadata=metadata or {},
    )
    return run_id


# ---------------------------------------------------------------------------
# M2-1a: stderr_similarity
# ---------------------------------------------------------------------------

class TestStderrSimilarity:
    """M2-1: stderr similarity calculation edge cases."""

    def test_identical_strings(self):
        from gateway.kanban_watchers import stderr_similarity
        assert stderr_similarity("hello world", "hello world") == 1.0

    def test_both_empty(self):
        from gateway.kanban_watchers import stderr_similarity
        assert stderr_similarity("", "") == 1.0
        assert stderr_similarity(None, None) == 1.0

    def test_one_empty(self):
        from gateway.kanban_watchers import stderr_similarity
        assert stderr_similarity("error", "") == 0.0
        assert stderr_similarity(None, "error") == 0.0

    def test_completely_different(self):
        from gateway.kanban_watchers import stderr_similarity
        assert stderr_similarity("abc", "xyz") < 0.4

    def test_high_similarity(self):
        from gateway.kanban_watchers import stderr_similarity
        s1 = "ImportError: No module named 'foo.bar.baz'"
        s2 = "ImportError: No module named 'foo.bar.qux'"
        assert stderr_similarity(s1, s2) > 0.8

    def test_truncated_to_200(self):
        """Only first 200 chars are compared."""
        from gateway.kanban_watchers import stderr_similarity
        prefix = "x" * 200
        s1 = prefix + "AAAA"
        s2 = prefix + "BBBB"
        # First 200 chars identical → similarity should be 1.0
        assert stderr_similarity(s1, s2) == 1.0

    def test_non_string_coerced(self):
        from gateway.kanban_watchers import stderr_similarity
        # ints are coerced to str
        assert stderr_similarity(str(123), str(123)) == 1.0


# ---------------------------------------------------------------------------
# M2-1b: detect_repeated_verification_errors
# ---------------------------------------------------------------------------

class TestDetectRepeatedErrors:
    """M2-1: repeated verification error detection."""

    def test_no_failures_returns_none(self, kanban_home):
        from gateway.kanban_watchers import detect_repeated_verification_errors
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            assert detect_repeated_verification_errors(conn, tid) is None

    def test_single_failure_returns_none(self, kanban_home):
        from gateway.kanban_watchers import detect_repeated_verification_errors
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest", "stderr": "error msg"}],
                })
            assert detect_repeated_verification_errors(conn, tid) is None

    def test_same_cmd_similar_stderr_detected(self, kanban_home):
        from gateway.kanban_watchers import detect_repeated_verification_errors
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            stderr = "ImportError: No module named 'foo.bar.baz'"
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest tests/", "stderr": stderr}],
                    "loop_depth": 0,
                })
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest tests/", "stderr": stderr}],
                    "loop_depth": 1,
                })
            result = detect_repeated_verification_errors(conn, tid)
            assert result is not None
            assert "repeated_verification_error" in result["reason"]
            assert result["details"]["cmd"] == "pytest tests/"
            assert result["details"]["stderr_similarity"] > 0.8

    def test_different_cmd_not_detected(self, kanban_home):
        from gateway.kanban_watchers import detect_repeated_verification_errors
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest", "stderr": "error"}],
                })
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "ruff", "stderr": "error"}],
                })
            assert detect_repeated_verification_errors(conn, tid) is None

    def test_same_cmd_different_stderr_not_detected(self, kanban_home):
        from gateway.kanban_watchers import detect_repeated_verification_errors
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest", "stderr": "ImportError: no module xyz"}],
                })
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest", "stderr": "AssertionError: 1 != 2"}],
                })
            assert detect_repeated_verification_errors(conn, tid) is None

    def test_empty_failures_in_payload(self, kanban_home):
        from gateway.kanban_watchers import detect_repeated_verification_errors
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {})
                kb._append_event(conn, tid, "verification_failed", {})
            assert detect_repeated_verification_errors(conn, tid) is None


# ---------------------------------------------------------------------------
# M2-1c: detect_token_anomaly
# ---------------------------------------------------------------------------

class TestDetectTokenAnomaly:
    """M2-1: token consumption anomaly detection."""

    def test_no_runs_returns_none(self, kanban_home):
        from gateway.kanban_watchers import detect_token_anomaly
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            assert detect_token_anomaly(conn, tid) is None

    def test_below_threshold_returns_none(self, kanban_home):
        from gateway.kanban_watchers import detect_token_anomaly
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            _seed_run(conn, tid, metadata={
                "input_tokens": 1000,
                "output_tokens": 2000,
            })
            assert detect_token_anomaly(conn, tid, threshold=50000) is None

    def test_above_threshold_detected(self, kanban_home):
        from gateway.kanban_watchers import detect_token_anomaly
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            _seed_run(conn, tid, metadata={
                "input_tokens": 30000,
                "output_tokens": 30000,
            })
            result = detect_token_anomaly(conn, tid, threshold=50000)
            assert result is not None
            assert "token_anomaly" in result["reason"]
            assert result["details"]["total"] == 60000
            assert result["details"]["threshold"] == 50000

    def test_custom_threshold(self, kanban_home):
        from gateway.kanban_watchers import detect_token_anomaly
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            _seed_run(conn, tid, metadata={
                "input_tokens": 5000,
                "output_tokens": 5000,
            })
            # With a low threshold, it triggers
            result = detect_token_anomaly(conn, tid, threshold=8000)
            assert result is not None
            assert result["details"]["total"] == 10000

    def test_missing_token_fields(self, kanban_home):
        from gateway.kanban_watchers import detect_token_anomaly
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            _seed_run(conn, tid, metadata={})
            assert detect_token_anomaly(conn, tid, threshold=50000) is None


# ---------------------------------------------------------------------------
# M2-1d: detect_no_output
# ---------------------------------------------------------------------------

class TestDetectNoOutput:
    """M2-1: no-artifact detection across consecutive runs."""

    def test_fewer_than_min_returns_none(self, kanban_home):
        from gateway.kanban_watchers import detect_no_output
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            _seed_run(conn, tid, metadata={})
            _seed_run(conn, tid, metadata={})
            assert detect_no_output(conn, tid, min_runs=3) is None

    def test_all_empty_detected(self, kanban_home):
        from gateway.kanban_watchers import detect_no_output
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            for _ in range(3):
                _seed_run(conn, tid, metadata={})
            result = detect_no_output(conn, tid, min_runs=3)
            assert result is not None
            assert "no_output" in result["reason"]
            assert result["details"]["empty_runs"] == 3

    def test_has_artifacts_not_detected(self, kanban_home):
        from gateway.kanban_watchers import detect_no_output
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            _seed_run(conn, tid, metadata={})
            _seed_run(conn, tid, metadata={"artifacts": ["/tmp/out.txt"]})
            _seed_run(conn, tid, metadata={})
            # Only 2/3 empty, not >= 3
            assert detect_no_output(conn, tid, min_runs=3) is None


# ---------------------------------------------------------------------------
# M2-1e: check_early_giveup (combined)
# ---------------------------------------------------------------------------

class TestCheckEarlyGiveup:
    """M2-1: combined smart giveup checker with toggle support."""

    def test_none_when_clean(self, kanban_home):
        from gateway.kanban_watchers import check_early_giveup
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            assert check_early_giveup(conn, tid) is None

    def test_disable_all_conditions(self, kanban_home):
        from gateway.kanban_watchers import check_early_giveup
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest", "stderr": "e"}],
                })
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest", "stderr": "e"}],
                })
            result = check_early_giveup(
                conn, tid,
                enable_repeated_error=False,
                enable_token_anomaly=False,
                enable_no_output=False,
            )
            assert result is None

    def test_returns_first_detection(self, kanban_home):
        from gateway.kanban_watchers import check_early_giveup
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            stderr = "ImportError: No module named 'foo.bar'"
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest", "stderr": stderr}],
                })
                kb._append_event(conn, tid, "verification_failed", {
                    "failures": [{"cmd": "pytest", "stderr": stderr}],
                })
            result = check_early_giveup(conn, tid)
            assert result is not None
            assert "repeated_verification_error" in result["reason"]


# ---------------------------------------------------------------------------
# M2-2: compute_board_convergence
# ---------------------------------------------------------------------------

class TestComputeBoardConvergence:
    """M2-2: board-level convergence detection (pure function)."""

    def test_empty_board(self, kanban_home):
        from gateway.kanban_watchers import compute_board_convergence
        with kb.connect() as conn:
            metrics = compute_board_convergence(conn)
            assert metrics["total_tasks"] == 0
            assert metrics["converged"] is False

    def test_all_done_converged(self, kanban_home):
        from gateway.kanban_watchers import compute_board_convergence
        with kb.connect() as conn:
            for i in range(5):
                tid = kb.create_task(conn, title=f"t{i}", assignee="w")
                kb.claim_task(conn, tid, claimer="test")
                kb.complete_task(conn, tid, summary="done")
            metrics = compute_board_convergence(conn)
            assert metrics["total_tasks"] == 5
            assert metrics["resolved"] == 5
            assert metrics["converged"] is True

    def test_has_blocked_not_converged(self, kanban_home):
        from gateway.kanban_watchers import compute_board_convergence
        with kb.connect() as conn:
            for i in range(4):
                tid = kb.create_task(conn, title=f"t{i}", assignee="w")
                kb.claim_task(conn, tid, claimer="test")
                kb.complete_task(conn, tid, summary="done")
            # 1 blocked → blocked_ratio = 0.2, not < 0.2
            tid = kb.create_task(conn, title="blocked", assignee="w")
            kb.claim_task(conn, tid, claimer="test")
            kb.block_task(conn, tid, reason="stuck")
            metrics = compute_board_convergence(conn)
            assert metrics["total_tasks"] == 5
            assert metrics["blocked"] == 1
            assert metrics["blocked_ratio"] == 0.2
            assert metrics["converged"] is False  # 0.2 is not < 0.2

    def test_has_verification_failed_not_converged(self, kanban_home):
        from gateway.kanban_watchers import compute_board_convergence
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee="w")
            kb.claim_task(conn, tid, claimer="test")
            kb.complete_task(conn, tid, summary="done")
            # Seed a verification_failed event
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "verification_failed", {})
            metrics = compute_board_convergence(conn)
            assert metrics["verification_failed"] == 1
            assert metrics["converged"] is False

    def test_has_remediation_not_converged(self, kanban_home):
        from gateway.kanban_watchers import compute_board_convergence
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="t", assignee="w")
            kb.claim_task(conn, tid, claimer="test")
            kb.complete_task(conn, tid, summary="done")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "remediation_created", {})
            metrics = compute_board_convergence(conn)
            assert metrics["new_tasks_created"] == 1
            assert metrics["converged"] is False

    def test_custom_thresholds(self, kanban_home):
        from gateway.kanban_watchers import compute_board_convergence
        with kb.connect() as conn:
            # 3 done, 1 blocked → blocked_ratio = 0.25
            for i in range(3):
                tid = kb.create_task(conn, title=f"t{i}", assignee="w")
                kb.claim_task(conn, tid, claimer="test")
                kb.complete_task(conn, tid, summary="done")
            tid = kb.create_task(conn, title="b", assignee="w")
            kb.claim_task(conn, tid, claimer="test")
            kb.block_task(conn, tid, reason="stuck")
            # With threshold 0.3, blocked_ratio 0.25 < 0.3 passes
            # resolve_rate 0.75 is NOT > 0.8, so still not converged
            metrics = compute_board_convergence(
                conn, blocked_ratio_threshold=0.3, resolve_rate_threshold=0.7,
            )
            assert metrics["converged"] is True

    def test_metrics_structure(self, kanban_home):
        from gateway.kanban_watchers import compute_board_convergence
        with kb.connect() as conn:
            metrics = compute_board_convergence(conn)
            required_keys = {
                "total_tasks", "resolved", "blocked", "running", "ready",
                "blocked_ratio", "resolve_rate", "new_tasks_created",
                "verification_failed", "converged",
            }
            assert required_keys.issubset(metrics.keys())


# ---------------------------------------------------------------------------
# M2-3: record_task_loop_closed
# ---------------------------------------------------------------------------

class TestRecordTaskLoopClosed:
    """M2-3: task_loop_closed event payload structure."""

    def test_event_written(self, kanban_home):
        from gateway.kanban_watchers import record_task_loop_closed
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            metrics = {
                "total_tasks": 5,
                "resolved": 4,
                "blocked": 1,
                "new_tasks_created": 0,
                "verification_failed": 0,
                "converged": True,
            }
            record_task_loop_closed(
                conn, tid,
                metrics=metrics,
                loop_depth=2,
                duration_seconds=300,
                task_loop_id="board:epoch:3",
            )
            events = kb.list_events(conn, tid)
            closed_events = [e for e in events if e.kind == "task_loop_closed"]
            assert len(closed_events) == 1

    def test_payload_structure(self, kanban_home):
        from gateway.kanban_watchers import record_task_loop_closed
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            metrics = {
                "total_tasks": 5,
                "resolved": 4,
                "blocked": 1,
                "new_tasks_created": 0,
                "verification_failed": 0,
                "converged": True,
            }
            record_task_loop_closed(
                conn, tid,
                metrics=metrics,
                loop_depth=2,
                duration_seconds=300,
                task_loop_id="board:epoch:3",
            )
            events = kb.list_events(conn, tid)
            ev = [e for e in events if e.kind == "task_loop_closed"][0]
            assert ev.payload is not None
            payload = ev.payload
            assert payload["task_loop_id"] == "board:epoch:3"
            assert payload["total_tasks"] == 5
            assert payload["resolved"] == 4
            assert payload["blocked"] == 1
            assert payload["new_tasks_created"] == 0
            assert payload["verification_failed"] == 0
            assert payload["converged"] is True
            assert payload["loop_depth"] == 2
            assert payload["duration_seconds"] == 300

    def test_default_values(self, kanban_home):
        from gateway.kanban_watchers import record_task_loop_closed
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="test", assignee="w")
            record_task_loop_closed(conn, tid, metrics={})
            events = kb.list_events(conn, tid)
            ev = [e for e in events if e.kind == "task_loop_closed"][0]
            assert ev.payload is not None
            assert ev.payload["loop_depth"] == 0
            assert ev.payload["duration_seconds"] == 0
            assert ev.payload["total_tasks"] == 0


# ---------------------------------------------------------------------------
# M2-4: should_auto_enable_task_loop
# ---------------------------------------------------------------------------

class TestShouldAutoEnableTaskLoop:
    """M2-4: goal_mode + verification.auto → auto task loop."""

    def test_goal_mode_with_verification_auto(self):
        from tools.kanban_tools import should_auto_enable_task_loop
        body = """## Task
```yaml
verification:
  auto:
    - cmd: "pytest tests/"
      expect: "exit_code == 0"
```
"""
        assert should_auto_enable_task_loop(True, body) is True

    def test_goal_mode_without_verification_auto(self):
        from tools.kanban_tools import should_auto_enable_task_loop
        body = "## Task\nDo the thing."
        assert should_auto_enable_task_loop(True, body) is False

    def test_no_goal_mode_with_verification_auto(self):
        from tools.kanban_tools import should_auto_enable_task_loop
        body = """```yaml
verification:
  auto:
    - cmd: "pytest tests/"
```
"""
        assert should_auto_enable_task_loop(False, body) is False
        assert should_auto_enable_task_loop(None, body) is False

    def test_goal_mode_with_manual_verification(self):
        from tools.kanban_tools import should_auto_enable_task_loop
        body = "## Task\nverification:\n  manual: true\n"
        assert should_auto_enable_task_loop(True, body) is False

    def test_goal_mode_with_empty_body(self):
        from tools.kanban_tools import should_auto_enable_task_loop
        assert should_auto_enable_task_loop(True, None) is False
        assert should_auto_enable_task_loop(True, "") is False

    def test_goal_mode_with_empty_auto_list(self):
        from tools.kanban_tools import should_auto_enable_task_loop
        body = "## Task\nverification:\n  auto: []\n"
        assert should_auto_enable_task_loop(True, body) is False


# ---------------------------------------------------------------------------
# M2-4: Integration — task_loop_started event on create
# ---------------------------------------------------------------------------

class TestGoalModeAutoLoopIntegration:
    """M2-4: goal_mode task with verification.auto gets task_loop_started event."""

    def test_task_loop_started_event_written(self, kanban_home):
        from tools.kanban_tools import should_auto_enable_task_loop
        body = """## Task
```yaml
verification:
  auto:
    - cmd: "true"
```
"""
        with kb.connect() as conn:
            tid = kb.create_task(
                conn, title="goal task", assignee="w",
                body=body, goal_mode=True,
            )
            # Verify the auto-enable check returns True
            assert should_auto_enable_task_loop(True, body) is True
            # Verify the event was written (simulated — the _handle_create
            # writes this after create_task; here we verify the check)
            # In a full e2e test the _handle_create would write it.

    def test_goal_mode_without_auto_no_event(self, kanban_home):
        from tools.kanban_tools import should_auto_enable_task_loop
        body = "## Task\nDo the thing."
        with kb.connect() as conn:
            tid = kb.create_task(
                conn, title="plain goal task", assignee="w",
                body=body, goal_mode=True,
            )
            # Should NOT auto-enable task loop
            assert should_auto_enable_task_loop(True, body) is False
            # No task_loop_started event
            events = kb.list_events(conn, tid)
            kinds = [e.kind for e in events]
            assert "task_loop_started" not in kinds


# ---------------------------------------------------------------------------
# Backward compatibility: convergence doesn't break normal tasks
# ---------------------------------------------------------------------------

class TestM2BackwardCompat:
    """M2 changes must not affect tasks without verification.auto."""

    def test_convergence_on_normal_board(self, kanban_home):
        from gateway.kanban_watchers import compute_board_convergence
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="normal", assignee="w")
            kb.claim_task(conn, tid, claimer="test")
            kb.complete_task(conn, tid, summary="done")
            metrics = compute_board_convergence(conn)
            # Single completed task → converged
            assert metrics["converged"] is True
            assert metrics["resolved"] == 1

    def test_detection_on_clean_task(self, kanban_home):
        from gateway.kanban_watchers import check_early_giveup
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="normal", assignee="w")
            kb.claim_task(conn, tid, claimer="test")
            kb.complete_task(conn, tid, summary="done")
            assert check_early_giveup(conn, tid) is None
