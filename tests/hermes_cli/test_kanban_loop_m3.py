"""Tests for M3: Outer-ring learning — child verification failure → parent
notification + verification standard inheritance.

Covers all 5 acceptance criteria from the task spec:
  AC-1: child verify fail → parent comment appears
  AC-2: multiple children fail → parent receives multiple independent notifications
  AC-3: child created with parent verification → child auto-inherits
  AC-4: child explicitly declares verification → uses its own, no inheritance
  AC-5: parentless task → no notification, no inheritance

Run:
  cd /home/zml/workspace/hermes-agent
  venv/bin/python -m pytest tests/hermes_cli/test_kanban_loop_m3.py -v
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


def _list_comments(conn, task_id):
    """Return all comment bodies for a task."""
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? ORDER BY created_at",
        (task_id,),
    ).fetchall()
    return [r["body"] for r in rows]


# ---------------------------------------------------------------------------
# M3-1: _build_failure_summary
# ---------------------------------------------------------------------------

class TestBuildFailureSummary:
    """Unit tests for the concise failure summary builder."""

    def test_empty_failures(self):
        from gateway.kanban_watchers import _build_failure_summary
        result = _build_failure_summary([])
        assert "no command details" in result

    def test_single_failure(self):
        from gateway.kanban_watchers import _build_failure_summary
        result = _build_failure_summary([
            {"cmd": "pytest", "exit_code": 1, "stderr": "assert failed"},
        ])
        assert "`pytest`" in result
        assert "exit=1" in result
        assert "assert failed" in result

    def test_caps_at_three(self):
        from gateway.kanban_watchers import _build_failure_summary
        result = _build_failure_summary([
            {"cmd": f"cmd{i}", "exit_code": 1, "stderr": "err"} for i in range(5)
        ])
        assert "cmd0" in result
        assert "cmd2" in result
        assert "cmd3" not in result

    def test_truncates_stderr(self):
        from gateway.kanban_watchers import _build_failure_summary
        long_stderr = "x" * 500
        result = _build_failure_summary([
            {"cmd": "pytest", "exit_code": 1, "stderr": long_stderr},
        ])
        # stderr is truncated to 120 chars in the summary
        assert "x" * 120 in result
        assert len(result) < 200


# ---------------------------------------------------------------------------
# M3-1: notify_parents_on_verification_failure
# ---------------------------------------------------------------------------

class TestNotifyParentsOnVerificationFailure:
    """M3-1: child verification failure → parent notification."""

    def test_notifies_single_parent(self, kanban_home):
        """AC-1: child verify fail → parent comment appears."""
        from gateway.kanban_watchers import notify_parents_on_verification_failure
        with kb.connect() as conn:
            parent = kb.create_task(conn, title="Parent Task", assignee="w")
            child = kb.create_task(
                conn, title="Child Task", assignee="w",
                parents=(parent,),
            )
            notified = notify_parents_on_verification_failure(
                conn, kb, child,
                failures=[{"cmd": "pytest", "exit_code": 1, "stderr": "fail"}],
                loop_depth=0,
            )
            assert notified == 1
            comments = _list_comments(conn, parent)
            assert len(comments) == 1
            assert "Child Task" in comments[0]
            assert "验证失败" in comments[0]
            assert "pytest" in comments[0]

    def test_notifies_multiple_parents(self, kanban_home):
        """AC-1 extended: multiple parents each get notified."""
        from gateway.kanban_watchers import notify_parents_on_verification_failure
        with kb.connect() as conn:
            p1 = kb.create_task(conn, title="P1", assignee="w")
            p2 = kb.create_task(conn, title="P2", assignee="w")
            child = kb.create_task(
                conn, title="Child", assignee="w",
                parents=(p1, p2),
            )
            notified = notify_parents_on_verification_failure(
                conn, kb, child,
                failures=[{"cmd": "make", "exit_code": 2, "stderr": "err"}],
                loop_depth=1,
            )
            assert notified == 2
            assert len(_list_comments(conn, p1)) == 1
            assert len(_list_comments(conn, p2)) == 1

    def test_multiple_children_each_notify(self, kanban_home):
        """AC-2: multiple children fail → parent gets multiple notifications."""
        from gateway.kanban_watchers import notify_parents_on_verification_failure
        with kb.connect() as conn:
            parent = kb.create_task(conn, title="Parent", assignee="w")
            c1 = kb.create_task(
                conn, title="Child A", assignee="w", parents=(parent,),
            )
            c2 = kb.create_task(
                conn, title="Child B", assignee="w", parents=(parent,),
            )
            notify_parents_on_verification_failure(
                conn, kb, c1,
                failures=[{"cmd": "cmd1", "exit_code": 1, "stderr": "e1"}],
                loop_depth=0,
            )
            notify_parents_on_verification_failure(
                conn, kb, c2,
                failures=[{"cmd": "cmd2", "exit_code": 1, "stderr": "e2"}],
                loop_depth=0,
            )
            comments = _list_comments(conn, parent)
            assert len(comments) == 2
            assert any("Child A" in c for c in comments)
            assert any("Child B" in c for c in comments)

    def test_no_parents_returns_zero(self, kanban_home):
        """AC-5: parentless task → no notification."""
        from gateway.kanban_watchers import notify_parents_on_verification_failure
        with kb.connect() as conn:
            child = kb.create_task(conn, title="Orphan", assignee="w")
            notified = notify_parents_on_verification_failure(
                conn, kb, child,
                failures=[{"cmd": "x", "exit_code": 1, "stderr": "e"}],
                loop_depth=0,
            )
            assert notified == 0

    def test_archived_parent_skipped(self, kanban_home):
        """Edge: parent archived → skip notification."""
        from gateway.kanban_watchers import notify_parents_on_verification_failure
        with kb.connect() as conn:
            parent = kb.create_task(conn, title="Archived Parent", assignee="w")
            child = kb.create_task(
                conn, title="Child", assignee="w", parents=(parent,),
            )
            # Archive the parent
            conn.execute(
                "UPDATE tasks SET status='archived' WHERE id=?", (parent,)
            )
            conn.commit()
            notified = notify_parents_on_verification_failure(
                conn, kb, child,
                failures=[{"cmd": "x", "exit_code": 1, "stderr": "e"}],
                loop_depth=0,
            )
            assert notified == 0
            assert len(_list_comments(conn, parent)) == 0

    def test_does_not_modify_parent_status(self, kanban_home):
        """Quality: notification does not change parent status."""
        from gateway.kanban_watchers import notify_parents_on_verification_failure
        with kb.connect() as conn:
            parent = kb.create_task(conn, title="Parent", assignee="w")
            child = kb.create_task(
                conn, title="Child", assignee="w", parents=(parent,),
            )
            # Set parent to done
            conn.execute(
                "UPDATE tasks SET status='done' WHERE id=?", (parent,)
            )
            conn.commit()
            notify_parents_on_verification_failure(
                conn, kb, child,
                failures=[{"cmd": "x", "exit_code": 1, "stderr": "e"}],
                loop_depth=0,
            )
            row = conn.execute(
                "SELECT status FROM tasks WHERE id=?", (parent,)
            ).fetchone()
            assert row["status"] == "done"

    def test_loop_depth_in_comment(self, kanban_home):
        """Notification includes loop depth (1-indexed for display)."""
        from gateway.kanban_watchers import notify_parents_on_verification_failure
        with kb.connect() as conn:
            parent = kb.create_task(conn, title="P", assignee="w")
            child = kb.create_task(
                conn, title="C", assignee="w", parents=(parent,),
            )
            notify_parents_on_verification_failure(
                conn, kb, child,
                failures=[{"cmd": "x", "exit_code": 1, "stderr": "e"}],
                loop_depth=2,
            )
            comments = _list_comments(conn, parent)
            assert "loop 3" in comments[0]  # loop_depth + 1


# ---------------------------------------------------------------------------
# M3-2: Verification standard inheritance
# ---------------------------------------------------------------------------

class TestVerificationInheritance:
    """M3-2: parent verification auto-inherited by child task."""

    PARENT_BODY_AUTO = """\
# Parent Task
Do the work.

```yaml
verification:
  auto:
    - cmd: "pytest tests/ -x -q"
      expect: "exit_code == 0"
```
"""

    PARENT_BODY_MANUAL = """\
# Parent Task

```yaml
verification:
  manual: true
```
"""

    PARENT_BODY_NO_VERIFICATION = """\
# Parent Task
Just do it.
"""

    def _do_inherit(self, conn, body, parents):
        """Run the inheritance function and return the resulting body."""
        from tools.kanban_tools import inherit_verification_from_parents
        return inherit_verification_from_parents(kb, conn, body, parents)

    def test_child_inherits_auto_verification(self, kanban_home):
        """AC-3: parent has verification.auto → child inherits."""
        with kb.connect() as conn:
            parent = kb.create_task(
                conn, title="Parent", assignee="w",
                body=self.PARENT_BODY_AUTO,
            )
            child_body = "# Child Task\nDo stuff.\n"
            result = self._do_inherit(conn, child_body, [parent])
            vconfig = kb.parse_verification_config(result)
            assert vconfig is not None, "Child should have inherited verification"
            assert isinstance(vconfig.get("auto"), list)
            assert len(vconfig["auto"]) == 1
            assert vconfig["auto"][0]["cmd"] == "pytest tests/ -x -q"

    def test_child_inherits_manual_verification(self, kanban_home):
        """AC-3 extended: parent has manual verification → child inherits manual."""
        with kb.connect() as conn:
            parent = kb.create_task(
                conn, title="Parent", assignee="w",
                body=self.PARENT_BODY_MANUAL,
            )
            child_body = "# Child Task\nDo stuff.\n"
            result = self._do_inherit(conn, child_body, [parent])
            vconfig = kb.parse_verification_config(result)
            assert vconfig is not None
            assert vconfig.get("manual") is True

    def test_child_overrides_with_own_verification(self, kanban_home):
        """AC-4: child explicitly declares verification → uses its own."""
        with kb.connect() as conn:
            parent = kb.create_task(
                conn, title="Parent", assignee="w",
                body=self.PARENT_BODY_AUTO,
            )
            child_body = """\
# Child Task

```yaml
verification:
  auto:
    - cmd: "ruff check ."
      expect: "exit_code == 0"
```
"""
            result = self._do_inherit(conn, child_body, [parent])
            vconfig = kb.parse_verification_config(result)
            assert vconfig is not None
            cmds = [c["cmd"] for c in vconfig["auto"]]
            assert "ruff check ." in cmds
            assert "pytest tests/ -x -q" not in cmds

    def test_no_parent_no_inheritance(self, kanban_home):
        """AC-5: parentless task → no verification inheritance."""
        with kb.connect() as conn:
            child_body = "# Orphan Task\nDo stuff.\n"
            result = self._do_inherit(conn, child_body, [])
            vconfig = kb.parse_verification_config(result)
            assert vconfig is None

    def test_parent_no_verification_no_inheritance(self, kanban_home):
        """Parent has no verification → child gets nothing (backward compat)."""
        with kb.connect() as conn:
            parent = kb.create_task(
                conn, title="Parent", assignee="w",
                body=self.PARENT_BODY_NO_VERIFICATION,
            )
            child_body = "# Child Task\nDo stuff.\n"
            result = self._do_inherit(conn, child_body, [parent])
            vconfig = kb.parse_verification_config(result)
            assert vconfig is None

    def test_inherits_from_first_parent_with_verification(self, kanban_home):
        """Multiple parents: inherits from the first one that has verification."""
        with kb.connect() as conn:
            p1 = kb.create_task(
                conn, title="P1", assignee="w",
                body=self.PARENT_BODY_NO_VERIFICATION,
            )
            p2 = kb.create_task(
                conn, title="P2", assignee="w",
                body=self.PARENT_BODY_AUTO,
            )
            child_body = "# Child\nDo stuff.\n"
            result = self._do_inherit(conn, child_body, [p1, p2])
            vconfig = kb.parse_verification_config(result)
            assert vconfig is not None
            cmds = [c["cmd"] for c in vconfig["auto"]]
            assert "pytest tests/ -x -q" in cmds


# ---------------------------------------------------------------------------
# Integration: M3-1 + M3-2 end-to-end through _handle_verification_failure
# ---------------------------------------------------------------------------

class TestIntegrationVerificationFailureNotification:
    """Integration: _handle_verification_failure triggers M3-1 notification.

    This verifies the wiring between kanban_tools.py and kanban_watchers.py —
    when verification fails, parents get notified automatically.
    """

    def test_verification_failure_notifies_parent(self, kanban_home):
        """Full path: kanban_complete verification fail → parent comment."""
        from tools.kanban_tools import _handle_verification_failure
        with kb.connect() as conn:
            parent = kb.create_task(conn, title="Parent", assignee="w")
            child = kb.create_task(
                conn, title="Child", assignee="w", parents=(parent,),
            )
            # Simulate a verification failure
            _handle_verification_failure(
                kb, conn, child,
                kb.get_task(conn, child),
                failures=[{"cmd": "pytest", "exit_code": 1, "stderr": "err"}],
                summary="attempted but failed",
                expected_run_id=None,
            )
            # Parent should have received a notification comment
            comments = _list_comments(conn, parent)
            assert any("验证失败" in c for c in comments)
            assert any("Child" in c for c in comments)

    def test_verification_failure_orphan_no_notification(self, kanban_home):
        """Orphan task fails → no parent notification (no crash)."""
        from tools.kanban_tools import _handle_verification_failure
        with kb.connect() as conn:
            child = kb.create_task(conn, title="Orphan", assignee="w")
            # Should not raise
            _handle_verification_failure(
                kb, conn, child,
                kb.get_task(conn, child),
                failures=[{"cmd": "pytest", "exit_code": 1, "stderr": "err"}],
                summary="attempted but failed",
                expected_run_id=None,
            )
