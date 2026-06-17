"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the ``kanban`` toolset for
orchestrator work. A normal ``hermes chat`` session still sees **zero**
kanban tools in its schema unless configured.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


class _BoardSourceRegistry:
    """Thread-safe per-board ``(platform, chat_id)`` source registry.

    Replaces the bare module-level ``dict`` that was the source of
    Bug #1 (concurrent-write leakage): two dispatcher sessions writing
    the same board slug could interleave and leave the dict in a torn
    state. Every access is now guarded by a lock so concurrent
    ``_connect()`` writes from parallel workers never corrupt each other.

    The registry maps board slug → last ``(platform_str, chat_id)`` that
    operated the board. Last-write-wins per slug is the *intended*
    semantic: the most recent active session is the one the epoch
    callback should route notifications to. Different slugs never
    collide, so two workers on two boards are fully isolated.
    """

    def __init__(self) -> None:
        self._sources: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def set(self, board: str, platform: str, chat_id: str) -> None:
        with self._lock:
            self._sources[board] = (platform, chat_id)

    def get(self, board: str) -> Optional[tuple[str, str]]:
        with self._lock:
            return self._sources.get(board)

    def clear(self, board: Optional[str] = None) -> None:
        with self._lock:
            if board is None:
                self._sources.clear()
            else:
                self._sources.pop(board, None)

    def snapshot(self) -> dict[str, tuple[str, str]]:
        """Return a shallow copy (safe to iterate outside the lock)."""
        with self._lock:
            return dict(self._sources)


# Module-level singleton. Written by kanban tool handlers via _connect()
# (reading the per-session ContextVar), read by the gateway epoch callback
# via resolve_board_source() to route notifications.
_board_source_registry = _BoardSourceRegistry()
# Backward-compat alias for any out-of-tree code that still reads the old
# bare-dict name. It is NOT a dict — access goes through the registry's
# thread-safe methods. The epoch callback and _connect() use the registry
# directly; this alias only exists so a stray ``_kt._kanban_board_sources``
# reference doesn't AttributeError at import time.
_kanban_board_sources = _board_source_registry


def resolve_board_source(slug: str) -> Optional[tuple[str, str]]:
    """Unified source resolution for epoch-notification routing.

    Returns ``(platform_str, chat_id)`` for *slug*, or ``None`` when no
    source can be determined.  Lookup order:

    1. **In-memory last-interaction source** — set by :func:`_connect`
       when a kanban *write* tool (create/complete/block) is invoked.
       This records which session last operated the board.
    2. **Board notify subscription** — falls back to the first row in
       ``kanban_notify_subs`` for the board, so boards that were set up
       via ``/kanban create`` but never had a tool write still route
       correctly.

    This replaces the dual-path inline lookup that previously lived in
    the epoch callback (Bug #3) and the ``_kanban_last_user_source``
    alias in run.py (Bug #4).
    """
    # Primary: in-memory last-interaction source (thread-safe registry).
    src = _board_source_registry.get(slug)
    if src and src[0]:
        return src

    # Fallback: board's active notify subscription.
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect(board=slug)
        try:
            subs = kb.list_notify_subs(conn)
            if subs:
                s = subs[0]
                sp = (s.get("platform") or "").lower()
                sch = s.get("chat_id") or ""
                if sp and sch:
                    return (sp, sch)
        finally:
            conn.close()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None, *, update_source: bool = False):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.

    ``update_source=True`` records this (platform, chat_id) as the board's
    epoch push target. Only set this for write operations (create, complete,
    block, etc.) — read-only ops (list, show) must NOT hijack the channel.
    """
    from hermes_cli import kanban_db as kb
    conn = kb.connect(board=board)
    if update_source:
        try:
            resolved = kb.get_current_board() if not board else board
            # Prefer ContextVar (per-session, concurrent-safe) over the
            # process-wide registry. The registry write below is thread-safe
            # (Bug #1 fix); the ContextVar isolates which session's source
            # is recorded so two concurrent workers on different boards never
            # cross-contaminate.
            _current_src = None
            try:
                from gateway.session_context import _KANBAN_SOURCE_PLATFORM, _KANBAN_SOURCE_CHAT
                _p = _KANBAN_SOURCE_PLATFORM.get()
                _c = _KANBAN_SOURCE_CHAT.get()
                if _p and _c:
                    _current_src = (_p, _c)
            except Exception:
                pass
            if _current_src and resolved:
                _plat, _chat = _current_src
                _board_source_registry.set(resolved, _plat, _chat)
        except Exception:
            pass
    return kb, conn


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per-process; runtime activity
#     can tick on every chunk/tool result and we don't need that resolution.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - No durable note on these auto-heartbeats; that's reserved for the
#     explicit tool which carries a model-supplied note.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env() -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True if a write was attempted (whether or not it succeeded);
    False if the call was skipped (not a kanban worker, rate-limited, or
    swallowed exception). The boolean is informational — callers should
    not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited via the module-level ``_auto_heartbeat_last_attempt``
    timestamp (monotonic clock); not thread-safe in the strict sense, but
    the worst case is one extra DB write per race, which is harmless.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                kb.heartbeat_worker(conn, tid, note=None, expected_run_id=run_id)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            comments = kb.list_comments(conn, tid)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                    "model_override": t.model_override,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            return json.dumps({
                "task": _task_dict(task),
                "verification": kb.parse_verification_config(task.body),
                "parents": parents,
                "children": children,
                "comments": [
                    {"author": c.author, "body": c.body,
                     "created_at": c.created_at}
                    for c in comments
                ],
                "events": [
                    {"kind": e.kind, "payload": e.payload,
                     "created_at": e.created_at, "run_id": e.run_id}
                    for e in events[-50:]   # cap; full log via CLI
                ],
                "runs": [_run_dict(r) for r in runs],
                # Also surface the worker's own context block so the
                # agent can include it directly if it wants. This is
                # the same string build_worker_context returns to the
                # dispatcher at spawn time.
                "worker_context": kb.build_worker_context(conn, tid),
            })
        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _run_auto_verification(
    workspace: str, auto_cmds: list,
) -> list[dict]:
    """Run ``verification.auto`` commands and return structured results.

    Each command is executed via ``subprocess.run`` in the task workspace
    so the verification environment matches the worker's.  Returns a list
    of ``{"cmd", "exit_code", "stdout", "stderr", "passed"}`` dicts — one
    per command.  ``passed`` is True iff ``exit_code == 0`` (the MVP
    supports ``expect: "exit_code == 0"`` only).
    """
    import subprocess

    cwd = workspace if workspace and os.path.isdir(workspace) else None
    results: list[dict] = []
    for cmd_spec in auto_cmds:
        if not isinstance(cmd_spec, dict):
            continue
        cmd = str(cmd_spec.get("cmd", "")).strip()
        if not cmd:
            continue
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            results.append({
                "cmd": cmd,
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-2000:],
                "passed": proc.returncode == 0,
            })
        except subprocess.TimeoutExpired:
            results.append({
                "cmd": cmd,
                "exit_code": -1,
                "stdout": "",
                "stderr": "timeout after 120s",
                "passed": False,
            })
        except Exception as exc:
            results.append({
                "cmd": cmd,
                "exit_code": -1,
                "stdout": "",
                "stderr": str(exc),
                "passed": False,
            })
    return results


def should_auto_enable_task_loop(
    goal_mode: Optional[bool], body: Optional[str],
) -> bool:
    """Determine whether a goal_mode task should auto-enter the task loop (M2-4).

    Returns ``True`` when **both** conditions hold:
    1. ``goal_mode`` is truthy (the task uses goal-turn budget).
    2. The body declares ``verification.auto`` commands.

    When the body has no ``verification.auto`` block, goal_mode still works
    via the existing goal-turn budget mechanism — the two are complementary:
    the task loop manages the verification closed-loop (verify → remediate →
    re-verify), while goal-turn manages conversation turn budget.
    """
    if not goal_mode:
        return False
    if not body:
        return False
    from hermes_cli import kanban_db as _kb
    vconfig = _kb.parse_verification_config(body)
    return (
        vconfig is not None
        and isinstance(vconfig.get("auto"), list)
        and len(vconfig["auto"]) > 0
    )


def inherit_verification_from_parents(
    kb, conn, body: Optional[str], parents: list,
) -> Optional[str]:
    """Return *body* with inherited verification config (M3-2).

    If *body* already declares its own ``verification`` block, it is
    returned unchanged (child override).  Otherwise the function scans
    *parents* in order and, upon finding the first parent whose body
    declares a verification config, appends that config to *body* as a
    YAML block.

    Inheritance logic::

        child_verification = child_body.get('verification') or parent_body.get('verification')

    - Parent has ``verification.auto``  → child gets the same auto commands.
    - Parent has ``verification.manual`` → child gets manual.
    - Parent has no verification         → child gets nothing (backward compat).
    """
    if not body or not parents:
        return body
    child_vconfig = kb.parse_verification_config(body)
    if child_vconfig is not None:
        return body  # child already has its own verification
    for pid in parents:
        try:
            ptask = kb.get_task(conn, pid)
            if ptask and ptask.body:
                pvconfig = kb.parse_verification_config(ptask.body)
                if pvconfig:
                    import yaml as _yaml
                    v_yaml = _yaml.dump(
                        {"verification": pvconfig},
                        default_flow_style=False,
                        allow_unicode=True,
                    ).strip()
                    logger.info(
                        "M3-2: child task inherited verification from parent %s",
                        pid,
                    )
                    return body.rstrip() + "\n\n" + v_yaml + "\n"
        except Exception:
            logger.debug(
                "M3-2: verification inheritance from parent %s failed",
                pid, exc_info=True,
            )
    return body


def _handle_verification_failure(
    kb, conn, tid, task, failures: list[dict], *,
    summary: str, expected_run_id,
) -> str:
    """Handle a verification failure: write events + reopen or block.

    Implements M1-3 (feedback injection), M1-4 (spiral convergence
    reopen), and M1-5 (loop-limit → block).  The decision tree:

    1. Count prior ``verification_failed`` events → ``loop_depth``.
    2. If ``loop_depth >= max_loop_depth`` → block with loop history.
    3. Otherwise → reopen for auto-remediation (task → ready).
    4. Either way, write ``verification_failed`` + ``feedback_provided``
       events so ``build_worker_context`` can inject structured feedback
       into the next remediation worker.
    """
    max_loop_depth = int(
        os.environ.get("HERMES_KANBAN_MAX_LOOP_DEPTH", "3")
    )
    loop_depth = kb.count_verification_loops(conn, tid)
    feedback = {
        "failures": failures,
        "previous_attempt": (summary or "").strip()[:2000],
        "loop_depth": loop_depth,
    }

    # Write verification_failed + feedback_provided events.
    vfail_payload = {
        "failures": failures,
        "previous_attempt": feedback["previous_attempt"],
        "loop_depth": loop_depth,
        "max_loop_depth": max_loop_depth,
    }
    with kb.write_txn(conn):
        kb._append_event(
            conn, tid, "verification_failed", vfail_payload,
            run_id=kb._current_run_id(conn, tid),
        )
        kb._append_event(
            conn, tid, "feedback_provided", feedback,
            run_id=kb._current_run_id(conn, tid),
        )

    # M3-1: Notify parent tasks about this verification failure.
    # Notification-only — does not modify parent status.
    try:
        from gateway.kanban_watchers import notify_parents_on_verification_failure
        notify_parents_on_verification_failure(
            conn, kb, tid,
            failures=failures,
            loop_depth=loop_depth,
        )
    except Exception:
        logger.debug(
            "M3-1: parent notification skipped for %s", tid, exc_info=True,
        )

    if loop_depth >= max_loop_depth:
        # M1-5: Loop limit reached — block with loop history summary.
        loop_history = []
        for i, f in enumerate(failures, 1):
            loop_history.append(
                f"  {i}. `{f.get('cmd', '?')}` exit={f.get('exit_code', '?')}"
            )
        reason = (
            f"verification failed {loop_depth + 1} times "
            f"(max_loop_depth={max_loop_depth}). "
            f"Loop history:\n" + "\n".join(loop_history)
        )
        kb.block_task(
            conn, tid, reason=reason,
            expected_run_id=expected_run_id,
        )
        return tool_error(
            f"kanban_complete: verification FAILED (loop {loop_depth + 1}/"
            f"{max_loop_depth}). Loop limit reached — task blocked. "
            f"Failures:\n" + "\n".join(
                f"  - `{f.get('cmd', '?')}`: exit={f.get('exit_code', '?')}, "
                f"stderr={f.get('stderr', '')[:200]}"
                for f in failures
            )
        )

    # M1-4: Reopen for auto-remediation (task → ready, same workspace).
    ok = kb.reopen_for_remediation(
        conn, tid,
        loop_depth=loop_depth,
        failures=failures,
        feedback=feedback,
        expected_run_id=expected_run_id,
    )
    if not ok:
        return tool_error(
            f"kanban_complete: verification FAILED but could not reopen "
            f"task {tid} for remediation (state transition failed). "
            f"Manual intervention required."
        )
    return tool_error(
        f"kanban_complete: verification FAILED (loop {loop_depth + 1}/"
        f"{max_loop_depth}). Task reopened for auto-remediation. "
        f"The dispatcher will re-dispatch you with structured feedback. "
        f"Failures:\n" + "\n".join(
            f"  - `{f.get('cmd', '?')}`: exit={f.get('exit_code', '?')}, "
            f"stderr={f.get('stderr', '')[:200]}"
            for f in failures
        )
    )


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board, update_source=True)
        try:
            # --- M1: Verification gate (M1-2/M1-3/M1-4/M1-5) ----------
            # If the task body declares ``verification.auto``, run the
            # verification commands BEFORE accepting the completion.
            # Failure → reopen or block (spiral convergence).
            # Success → fall through to normal complete_task, then
            # write a ``verified`` event.
            _verification_passed = False
            task = kb.get_task(conn, tid)
            if task:
                vconfig = kb.parse_verification_config(task.body)
                if (
                    vconfig
                    and isinstance(vconfig.get("auto"), list)
                    and vconfig["auto"]
                ):
                    workspace = (
                        task.workspace_path
                        or os.environ.get("HERMES_KANBAN_WORKSPACE", "")
                    )
                    results = _run_auto_verification(
                        workspace, vconfig["auto"]
                    )
                    failures = [r for r in results if not r["passed"]]
                    if failures:
                        return _handle_verification_failure(
                            kb, conn, tid, task, failures,
                            summary=summary or result or "",
                            expected_run_id=_worker_run_id(tid),
                        )
                    _verification_passed = True
            # --- End verification gate --------------------------------

            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            # M1-2: Write ``verified`` event when auto-verification passed.
            if _verification_passed:
                with kb.write_txn(conn):
                    kb._append_event(
                        conn, tid, "verified",
                        {"auto": True},
                        run_id=kb._current_run_id(conn, tid),
                    )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_complete: {e}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board, update_source=True)
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    board = args.get("board")
    try:
        kb, conn = _connect(board=board, update_source=True)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    session_id = args.get("session_id") or os.environ.get("HERMES_SESSION_ID")
    priority = args.get("priority")
    # Resolve workspace. If the caller passed one explicitly, honor it.
    # Otherwise, a dispatcher-spawned worker (HERMES_KANBAN_TASK set)
    # inherits its own running task's workspace, so a worker editing a
    # dir:/worktree project that spawns a follow-up child keeps the child
    # in that project instead of a throwaway scratch dir. Orchestrators
    # (kanban toolset, no HERMES_KANBAN_TASK) and CLI/dashboard callers
    # fall back to scratch as before. Explicit None path stays None.
    workspace_kind = args.get("workspace_kind")
    workspace_path = args.get("workspace_path")
    _inherit_workspace = workspace_kind is None and workspace_path is None
    if workspace_kind is None:
        workspace_kind = "scratch"
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    initial_status = args.get("initial_status") or "running"
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    goal_mode, goal_bool_error = _parse_bool_arg(args, "goal_mode")
    if goal_bool_error:
        return tool_error(goal_bool_error)
    goal_max_turns = args.get("goal_max_turns")
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board, update_source=True)
        try:
            # Inherit the spawning worker's own task workspace when the
            # caller didn't specify one (see resolution note above).
            if _inherit_workspace:
                _self_tid = os.environ.get("HERMES_KANBAN_TASK")
                if _self_tid:
                    _self_task = kb.get_task(conn, _self_tid)
                    if _self_task is not None and _self_task.workspace_kind:
                        workspace_kind = _self_task.workspace_kind
                        workspace_path = _self_task.workspace_path

            # M3-2: Verification standard inheritance.
            # If the child body doesn't declare its own verification block,
            # copy the first parent's verification config into the child body.
            # Child can override by explicitly declaring verification.
            body = inherit_verification_from_parents(kb, conn, body, list(parents))

            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                goal_mode=goal_mode,
                goal_max_turns=(
                    int(goal_max_turns) if goal_max_turns is not None else None
                ),
                initial_status=str(initial_status),
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
            )
            new_task = kb.get_task(conn, new_tid)
            # M2-4: goal_mode + verification.auto → auto-enable task loop.
            # Write a task_loop_started event so the EpochEngine and
            # downstream consumers know this task entered the
            # verification closed-loop, not just the goal-turn budget.
            if should_auto_enable_task_loop(goal_mode, body):
                try:
                    with kb.write_txn(conn):
                        kb._append_event(
                            conn, new_tid, "task_loop_started",
                            {
                                "goal_mode": True,
                                "verification_auto": True,
                                "auto_loop": True,
                            },
                        )
                    logger.info(
                        "kanban_create: task %s auto-enabled task loop "
                        "(goal_mode + verification.auto)",
                        new_tid,
                    )
                except Exception as tl_exc:
                    logger.debug(
                        "kanban_create: task_loop_started event failed for %s: %s",
                        new_tid, tl_exc,
                    )
            return _ok(
                task_id=new_tid,
                status=new_task.status if new_task else None,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task back to ready."""
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board, update_source=True)
        try:
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            return _ok(task_id=str(tid), status="ready")
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board, update_source=True)
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Surfaced to downstream "
                    "workers alongside ``summary``."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk when the notifier runs; missing files "
                    "are silently skipped."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Transition the task to blocked because you need human input "
        "to proceed. ``reason`` will be shown to the human on the "
        "board and included in context when someone unblocks you. "
        "Use for genuine blockers only — don't block on things you can "
        "resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered, in one or two sentences. "
                    "Don't paste the whole conversation; the human has "
                    "the board and can ask follow-ups via comments."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker (in addition to the built-in kanban-worker "
                    "skill). Use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "goal_mode": {
                "type": "boolean",
                "description": (
                    "Run the dispatched worker in a goal loop. When true, "
                    "after each turn an auxiliary judge checks the worker's "
                    "response against this card's title/body; if the work "
                    "isn't done and budget remains, the worker keeps going "
                    "in the same session until the judge agrees it's "
                    "complete (or the goal-turn budget is exhausted, which "
                    "blocks the task for human review). Use this for "
                    "open-ended cards where one shot rarely finishes the "
                    "work. Defaults to false (classic single-shot worker)."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "description": (
                    "Turn budget for goal_mode workers. Caps how many "
                    "continuation turns the worker may take before the task "
                    "is blocked for review. Ignored unless goal_mode is "
                    "true. Defaults to the goal-engine default (20)."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Move a blocked Kanban task back to ready. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to return to ready.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)
