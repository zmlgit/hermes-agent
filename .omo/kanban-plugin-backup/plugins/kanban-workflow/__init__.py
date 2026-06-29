"""Kanban workflow plugin — task-loop automation, board focus, and subagent prop.

No core files are modified — the plugin layers entirely on upstream's API.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

log = logging.getLogger("kanban_workflow")

# ---------------------------------------------------------------------------
# Global State & Config
# ---------------------------------------------------------------------------

_actions_local = threading.local()

# Bridging ContextVar across ThreadPoolExecutor boundaries for subagents
_board_map_lock = threading.Lock()
_session_boards: Dict[str, str] = {}  # session_id -> board_slug

def _get_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import get_config
        return get_config().get("plugins", {}).get("kanban_workflow", {})
    except Exception:
        return {}

def _get_assignees() -> set[str]:
    cfg = _get_config()
    val = cfg.get("verify_assignees", ["coder", "dba"])
    return set(val) if isinstance(val, list) else {"coder", "dba"}

def _get_trivial_keywords() -> set[str]:
    cfg = _get_config()
    val = cfg.get("trivial_keywords", ["typo", "trivial", "rename", "cosmetic", "docs", "comment"])
    return set(k.lower() for k in val) if isinstance(val, list) else {"typo", "trivial", "rename", "cosmetic", "docs", "comment"}

# ---------------------------------------------------------------------------
# Action log helpers
# ---------------------------------------------------------------------------
def _get_actions() -> List[Dict[str, str]]:
    if not hasattr(_actions_local, "actions"):
        _actions_local.actions = []
    return _actions_local.actions

def _clear_actions() -> None:
    _actions_local.actions = []

def _append_action(action: str, **fields: str) -> None:
    entry: Dict[str, str] = {"action": action}
    entry.update(fields)
    _get_actions().append(entry)

# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------
def _parse_jsonish_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict): return result
    if not isinstance(result, str): return {}
    try:
        parsed = json.loads(result)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}

def _parse_task_id(result: Any) -> Optional[str]:
    parsed = _parse_jsonish_result(result)
    if parsed.get("status") == "error" or parsed.get("success") is False:
        return None
    for path in ("data.task_id", "task_id"):
        cur = parsed
        for p in path.split("."):
            cur = cur.get(p) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur:
            return cur
    return None

def _is_subscribed(result: Any) -> bool:
    parsed = _parse_jsonish_result(result)
    if not parsed: return True
    for path in ("data.subscribed", "subscribed"):
        cur = parsed
        for p in path.split("."):
            cur = cur.get(p) if isinstance(cur, dict) else None
        if isinstance(cur, bool):
            return cur
    return True

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _get_conn():
    from hermes_cli import kanban_db as _kb
    return _kb, _kb.connect(board=_kb.get_current_board())

def _child_ids(conn, task_id: str) -> List[str]:
    rows = conn.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,)).fetchall()
    return [r[0] if isinstance(r, tuple) else r["child_id"] for r in rows]

def _has_tester_child(conn, task_id: str) -> bool:
    children = _child_ids(conn, task_id)
    if not children: return False
    placeholders = ",".join("?" * len(children))
    row = conn.execute(
        f"SELECT COUNT(*) FROM tasks WHERE id IN ({placeholders}) "
        "AND assignee = 'tester' AND status != 'archived'", children
    ).fetchone()
    return bool(row and row[0] > 0)

def _is_trivial(title: str) -> bool:
    lower = title.lower()
    return any(kw in lower for kw in _get_trivial_keywords())

def _all_done(conn, task_ids: List[str]) -> bool:
    if not task_ids: return True
    placeholders = ",".join("?" * len(task_ids))
    rows = conn.execute(f"SELECT status FROM tasks WHERE id IN ({placeholders})", task_ids).fetchall()
    terminal = {"done", "archived"}
    return all((r[0] if isinstance(r, tuple) else r["status"]) in terminal for r in rows)

# ---------------------------------------------------------------------------
# Task-loop automation
# ---------------------------------------------------------------------------
def _auto_verify(kb, conn, task_id: str) -> Optional[str]:
    task = kb.get_task(conn, task_id)
    if not task or task.assignee not in _get_assignees():
        return None
    if _is_trivial(task.title or "") or _has_tester_child(conn, task_id):
        return None
    vid = kb.create_task(
        conn, title=f"Verify: {task.title}", assignee="tester", parents=[task_id],
        idempotency_key=f"auto-verify:{task_id}",
        body="Automatically created Task Loop verification task.\nPass criteria: build/tests or functional validation passes."
    )
    log.info("[Kanban Auto] Created verification %s for %s", vid, task_id)
    return vid

def _auto_unblock_children(kb, conn, task_id: str) -> List[str]:
    children = _child_ids(conn, task_id)
    if not children: return []
    placeholders = ",".join("?" * len(children))
    blocked = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders}) AND status = 'blocked'", children
    ).fetchall()
    unblocked = []
    for row in blocked:
        cid = row[0] if isinstance(row, tuple) else row["id"]
        parents = kb.parent_ids(conn, cid)
        if _all_done(conn, parents):
            kb.unblock_task(conn, cid)
            log.info("[Kanban Auto] Unblocked %s (all parents done)", cid)
            unblocked.append(cid)
    return unblocked

def _auto_complete_parent(kb, conn, task_id: str) -> List[str]:
    completed = []
    for pid in kb.parent_ids(conn, task_id):
        parent = kb.get_task(conn, pid)
        if not parent or parent.status in {"done", "archived"}: continue
        children = _child_ids(conn, pid)
        if _all_done(conn, children):
            kb.complete_task(conn, pid, summary=f"auto-completed (triggered by {task_id})")
            log.info("[Kanban Auto] Completed parent %s", pid)
            completed.append(pid)
    return completed

# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------
def _on_subagent_start(parent_session_id: str = "", child_session_id: str = "", **_unused: Any) -> None:
    """Propagate the parent's focused board to the child agent."""
    if not parent_session_id or not child_session_id:
        return
    try:
        from hermes_cli.kanban_db import _CURRENT_BOARD_OVERRIDE
        current = _CURRENT_BOARD_OVERRIDE.get()
        if current:
            with _board_map_lock:
                _session_boards[child_session_id] = current
                _session_boards[parent_session_id] = current
    except Exception as exc:
        log.warning("subagent_start propagation failed: %s", exc)

def _on_pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, session_id: str = "", **_unused: Any) -> None:
    if not tool_name or not tool_name.startswith("kanban_"): return
    _clear_actions()
    
    try:
        from hermes_cli.kanban_db import _CURRENT_BOARD_OVERRIDE
        board = args.get("board") if args else None
        
        # 1. Explicit board passed -> latch it
        if board and isinstance(board, str) and board.strip():
            board = board.strip()
            _CURRENT_BOARD_OVERRIDE.set(board)
            if session_id:
                with _board_map_lock:
                    _session_boards[session_id] = board
        # 2. No explicit board -> check inherited dict
        else:
            if session_id and not _CURRENT_BOARD_OVERRIDE.get():
                with _board_map_lock:
                    inherited = _session_boards.get(session_id)
                if inherited:
                    _CURRENT_BOARD_OVERRIDE.set(inherited)
    except Exception:
        pass

def _on_post_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, result: Any = None, **_unused: Any) -> None:
    if tool_name != "kanban_create": return
    task_id = _parse_task_id(result)
    if not task_id or _is_subscribed(result): return
    parents = [p for p in ((args or {}).get("parents") or []) if p]
    if not parents: return
    try:
        kb, conn = _get_conn()
        try:
            existing = conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE task_id = ?", (task_id,)).fetchone()
            if existing and existing[0] > 0: return
            parent_subs = conn.execute(
                f"SELECT platform, chat_id, thread_id, user_id, notifier_profile FROM kanban_notify_subs WHERE task_id IN ({','.join('?' * len(parents))})",
                parents
            ).fetchall()
            for sub in parent_subs:
                kb.add_notify_sub(conn, task_id=task_id, platform=sub[0] if isinstance(sub, tuple) else sub["platform"], chat_id=sub[1] if isinstance(sub, tuple) else sub["chat_id"], thread_id=sub[2] if isinstance(sub, tuple) else sub["thread_id"], user_id=sub[3] if isinstance(sub, tuple) else sub["user_id"], notifier_profile=sub[4] if isinstance(sub, tuple) else sub["notifier_profile"])
        finally:
            conn.close()
    except Exception as exc:
        log.warning("[Kanban Auto] parent-notify failed for %s: %s", task_id, exc)

def _on_kanban_task_completed(task_id: str = "", **_unused: Any) -> None:
    if not task_id: return
    try:
        kb, conn = _get_conn()
        try:
            if vid := _auto_verify(kb, conn, task_id):
                _append_action("auto_verify", created_task=vid)
            for cid in _auto_unblock_children(kb, conn, task_id):
                _append_action("auto_unblock", unblocked_task=cid)
            for pid in _auto_complete_parent(kb, conn, task_id):
                _append_action("auto_complete_parent", completed_parent=pid)
        finally:
            conn.close()
    except Exception as exc:
        log.warning("[Kanban Auto] task-loop failed for %s: %s", task_id, exc)

def _on_transform_tool_result(tool_name: str = "", result: Any = None, **_unused: Any) -> Optional[str]:
    if tool_name != "kanban_complete": return None
    actions = _get_actions()
    _clear_actions()
    if not actions or not isinstance(result, str): return None
    try:
        parsed = json.loads(result)
        if not isinstance(parsed, dict): return None
        parsed["workflow_actions"] = actions
        return json.dumps(parsed, ensure_ascii=False)
    except Exception:
        return None

def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("kanban_task_completed", _on_kanban_task_completed)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    ctx.register_hook("subagent_start", _on_subagent_start)
    log.info("kanban-workflow registered")
