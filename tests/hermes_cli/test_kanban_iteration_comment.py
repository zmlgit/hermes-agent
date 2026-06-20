"""M6 (DESIGN.md §5): iteration-process transparency — auto-written comments.

Covers:
  * ``build_iteration_comment`` format + ≤200-char constraint (pure unit).
  * crash → ready (retry) writes a "失败 | 自动重试" iteration comment.
  * protocol-violation crash (rc=0, no transition) → blocked → coordinator
    takeover comment.
  * crash hitting the breaker limit → blocked → gave_up comment.
  * verification failure (M3-1 hook) writes an iteration comment on the
    failing child task.
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _exited_status(code: int) -> int:
    """Raw wait-status for a WIFEXITED child with the given exit code."""
    return code << 8


def _system_comments(conn, task_id: str) -> list[str]:
    """Bodies of comments written by the iteration-comment system author."""
    rows = conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND author = ? "
        "ORDER BY created_at ASC",
        (task_id, kb._ITERATION_COMMENT_AUTHOR),
    ).fetchall()
    return [r[0] for r in rows]


def _claim_running(conn, tid: str, pid: int, host: str, tag: str = "w"):
    conn.execute(
        "UPDATE tasks SET status='running', worker_pid=?, "
        "claim_lock=? WHERE id=?",
        (pid, f"{host}:{tag}", tid),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Pure formatter
# ---------------------------------------------------------------------------

def test_build_iteration_comment_format():
    line = kb.build_iteration_comment(2, "worker 崩溃", "失败", "自动重试")
    assert line.startswith("🔄 迭代 #2 | ")
    assert "尝试: worker 崩溃" in line
    assert "结果: 失败" in line
    assert "下一步: 自动重试" in line
    # exactly three pipe-separated payload fields after the prefix
    payload = line.split(" | ", 1)[1]
    assert payload.count(" | ") == 2


def test_build_iteration_comment_clamps_iteration_num():
    assert kb.build_iteration_comment(0, "x", "y", "z").startswith("🔄 迭代 #1 | ")
    assert kb.build_iteration_comment(-3, "x", "y", "z").startswith("🔄 迭代 #1 | ")


def test_build_iteration_comment_strips_pipes_and_newlines():
    line = kb.build_iteration_comment(1, "a|b\nc", "r", "s")
    # pipes/newlines inside fields must not corrupt the structure
    assert "|" not in line.split("🔄 迭代 #1 | ", 1)[1].split("尝试: ", 1)[1].split(" | 结果")[0]
    assert "\n" not in line


def test_build_iteration_comment_under_200_chars():
    line = kb.build_iteration_comment(1, "短", "短", "短")
    assert len(line) <= 200


def test_build_iteration_comment_truncates_long_fields():
    huge = "X" * 500
    line = kb.build_iteration_comment(3, huge, huge, huge)
    assert len(line) <= 200, f"comment {len(line)} chars exceeds 200"
    assert line.startswith("🔄 迭代 #3 | ")
    # structure survives truncation
    assert "尝试: " in line and "结果: " in line and "下一步: " in line


# ---------------------------------------------------------------------------
# Kernel hooks: crash / retry / takeover / gave_up
# ---------------------------------------------------------------------------

def test_crash_retry_writes_iteration_comment(kanban_home, monkeypatch):
    """An isolated crash (below the limit) requeues to ready AND writes a
    retry-shaped iteration comment."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="retry-comment", assignee="a")
        pid = 50001
        _claim_running(conn, tid, pid, host)
        _kb._record_worker_exit(pid, _exited_status(1))  # nonzero → crash

        crashed = kb.detect_crashed_workers(conn)
        assert tid in crashed

        task = kb.get_task(conn, tid)
        assert task.status == "ready"  # below limit → retry

        comments = _system_comments(conn, tid)
        assert len(comments) == 1
        c = comments[0]
        assert c.startswith("🔄 迭代 #1 | ")
        assert "尝试: worker 崩溃" in c
        assert "结果: 失败" in c
        assert "下一步: 自动重试" in c
        assert len(c) <= 200


def test_protocol_violation_writes_takeover_comment(kanban_home, monkeypatch):
    """A clean-exit-without-transition crash trips the breaker immediately and
    writes a coordinator-takeover iteration comment."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="proto-violation", assignee="a")
        pid = 50002
        _claim_running(conn, tid, pid, host)
        _kb._record_worker_exit(pid, _exited_status(0))  # rc=0 → clean exit

        kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"  # protocol violation → instant block

        comments = _system_comments(conn, tid)
        assert len(comments) == 1
        c = comments[0]
        assert c.startswith("🔄 迭代 #1 | ")
        assert "协议违规" in c
        assert "coordinator 接管" in c
        assert "等待人工完成" in c
        assert len(c) <= 200


def test_crash_limit_writes_gave_up_comment(kanban_home, monkeypatch):
    """Repeated crashes that reach the breaker limit block the task and write a
    gave_up iteration comment."""
    import hermes_cli.kanban_db as _kb

    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")

    with kb.connect() as conn:
        host = _kb._claimer_id().split(":", 1)[0]
        tid = kb.create_task(conn, title="gave-up-comment", assignee="a")

        for i in range(kb.DEFAULT_FAILURE_LIMIT):  # 2
            pid = 51000 + i
            _claim_running(conn, tid, pid, host, tag=f"w{i}")
            _kb._record_worker_exit(pid, _exited_status(1))
            kb.detect_crashed_workers(conn)

        task = kb.get_task(conn, tid)
        assert task.status == "blocked"  # breaker tripped

        comments = _system_comments(conn, tid)
        assert len(comments) >= 1
        last = comments[-1]
        # The final iteration (at the limit) is a gave_up comment.
        assert "放弃（已达重试上限）" in last
        assert "等待人工介入" in last
        assert len(last) <= 200
        # Iteration number on the last comment reflects the failure count.
        assert last.startswith(f"🔄 迭代 #{kb.DEFAULT_FAILURE_LIMIT} | ")


# ---------------------------------------------------------------------------
# Verification-failure hook (M3-1 → M6)
# ---------------------------------------------------------------------------

def test_verification_failure_writes_child_iteration_comment(kanban_home):
    """notify_parents_on_verification_failure writes an iteration comment on the
    failing child task, even when the task has no parents."""
    from gateway import kanban_watchers as gw

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="verify-fail", assignee="a")

        notified = gw.notify_parents_on_verification_failure(
            conn, kb,
            task_id=tid,
            failures=[{"cmd": "pytest", "exit_code": 1, "stderr": "boom"}],
            loop_depth=1,  # → iteration #2
        )
        # No parents → parent-notification count is 0, but the child comment
        # was still written.
        assert notified == 0

        comments = _system_comments(conn, tid)
        assert len(comments) == 1
        c = comments[0]
        assert c.startswith("🔄 迭代 #2 | ")
        assert "尝试: 数据未通过校验" in c
        assert "结果: 验证失败" in c
        assert "下一步: 重新修复" in c
        assert len(c) <= 200
