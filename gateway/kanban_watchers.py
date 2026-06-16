"""Kanban board watcher methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the background-loop methods that subscribe to kanban boards, deliver
notifications/artifacts, and drive the multi-agent dispatcher. They use only
``self`` state, so they live on a mixin that ``GatewayRunner`` inherits — the
``self._kanban_*`` call sites resolve identically via the MRO, making this a
behavior-neutral move that lifts ~1,000 LOC out of run.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    # ── Orchestrator epoch callback state ────────────────────────────────
    _orch_cb_cooldowns: dict[str, float] = {}
    _orch_cb_stale_counts: dict[str, int] = {}
    _orch_epoch_counts: dict[str, int] = {}
    _orch_cb_last_event_id: dict[str, int] = {}

    # Per-board last kanban-interaction source (platform, chat_id).
    # Keyed by board slug — each board remembers which session last
    # operated on it via kanban tools. Updated only when kanban tools
    # are actually invoked, not on every message.
    _kanban_last_user_source: dict[str, tuple[str, str]] = {}


    async def _kanban_orchestrator_callback(
        self,
        deliveries: list[dict],
        kanban_cfg: dict,
    ) -> None:
        """Check boards for completed epochs and notify the orchestrator.

        Runs on every notifier tick, **independent of subscriptions**.
        For each board in ``orchestrator_boards`` (or all boards if not
        configured), checks whether the board has zero running tasks AND
        has had a recent terminal event (completed/blocked/crashed/gave_up/
        timed_out). If so, injects an internal MessageEvent into the
        orchestrator profile's session so it can plan the next epoch.

        This does NOT depend on kanban_notify_subs or kanban_board_subs —
        it scans the task tables directly. All connected home channels
        receive the epoch decision push automatically.

        As part of the callback, also auto-completes any parent task
        whose children are all in a terminal state (see
        :func:`hermes_cli.kanban_db.auto_complete_parents`). This is the
        Phase-1 wiring for "implement done + verify done → parent done
        automatically" — a parent card with two leaf children (one
        implementation card and one verification card) is marked
        ``done`` by the watcher without needing an explicit
        ``kanban_complete`` from the worker.

        Configuration (in config.yaml under ``kanban:``):

        - ``orchestrator_notify: true`` — enable this callback.
        - ``orchestrator_profile: <name>`` — profile to notify.
        - ``orchestrator_boards: <list>`` — board slug allowlist.
        - ``orchestrator_cooldown_seconds: 30`` — min seconds between
          notifications per board.
        - ``orchestrator_max_epochs: 10`` — max epoch notifications per
          board before the callback goes silent.
        - ``orchestrator_max_stale: 3`` — max consecutive stale triggers
          before the board is suppressed until a real event arrives.
        """
        cooldown_seconds = float(kanban_cfg.get("orchestrator_cooldown_seconds", 30))
        MAX_EPOCHS = int(kanban_cfg.get("orchestrator_max_epochs", 10))
        # Force-trigger threshold (Phase 2 rule 3). When a task on a
        # board has accumulated this many consecutive failures, the
        # epoch callback must inject LLM help *even if* the board has
        # other ready cards waiting — the failure pathology won't fix
        # itself.
        FORCE_FAILURE_THRESHOLD = int(
            kanban_cfg.get("orchestrator_force_failure_threshold", 3)
        )

        now = time.monotonic()

        candidate_boards = self._scan_candidate_boards(kanban_cfg)

        for slug in candidate_boards:
            # Cooldown gate.
            if now - self._orch_cb_cooldowns.get(slug, 0) < cooldown_seconds:
                continue

            last_eid = self._orch_cb_last_event_id.get(slug, 0)
            stats = self._detect_epoch(slug, last_eid, now=now, max_epochs=MAX_EPOCHS)
            if stats is None:
                continue

            # Phase 2 rule 1: skip when the ready queue is non-empty AND
            # there are no terminal events to react to. Dispatcher picks
            # up ready cards on its own tick; calling LLM here just
            # wastes tokens.
            #
            # Phase 2 rule 3: but if any task on this board has hit
            # ``consecutive_failures >= FORCE_FAILURE_THRESHOLD``, force
            # the trigger — failures don't heal themselves and the
            # LLM needs to step in even when other work is queued.
            force_urgent = self._has_force_failure(
                slug, FORCE_FAILURE_THRESHOLD,
            )
            if (
                stats["ready_count"] > 0
                and not stats["has_terminal_events"]
                and not force_urgent
            ):
                logger.debug(
                    "kanban orchestrator callback: board %s has %d ready card(s) "
                    "and no terminal events; skipping LLM injection (dispatcher "
                    "will handle it)",
                    slug, stats["ready_count"],
                )
                continue

            # Mark the stats dict so the message builder / injector can
            # tag the trigger as urgent and surface the failing task ids
            # to the orchestrator up front.
            stats["force_urgent"] = bool(force_urgent)
            if force_urgent:
                stats["force_failure_threshold"] = FORCE_FAILURE_THRESHOLD
                failing_ids = self._failing_task_ids(slug, FORCE_FAILURE_THRESHOLD)
                stats["failing_task_ids"] = failing_ids

            # Phase-1 parent auto-completion. If any of the events we
            # just observed were leaves of a parent card, the parent
            # may have just become eligible for an automatic
            # transition to ``done``. Returns the list of parents we
            # flipped so the notification can mention them.
            stats["auto_completed"] = self._auto_complete_parents(
                slug, stats["event_details"],
            )

            msg_text = self._build_epoch_message(slug, stats["event_details"], stats)
            self._inject_epoch(msg_text, slug, stats)

    def _auto_complete_parents(
        self, board_slug: str, event_details: list,
    ) -> list[str]:
        """Auto-complete any parent whose children all reached a terminal state.

        Walks the ``task_links`` graph upward from every task id in
        ``event_details`` and flips the corresponding parents to
        ``done`` when ALL of their children are in a terminal status.
        See :func:`hermes_cli.kanban_db.auto_complete_parents` for the
        gating rules (parent must be ``running``, all children must be
        terminal, all of the parent's own parents must be terminal).

        Returns the (possibly-empty) list of parent task ids the watcher
        just auto-completed. Failures are logged and swallowed — a
        broken auto-completion must never block the orchestrator
        callback or the downstream epoch notification.
        """
        if not event_details:
            return []
        from hermes_cli import kanban_db as _kb

        task_ids: list[str] = []
        for ed in event_details:
            tid = ed.get("task_id")
            if isinstance(tid, str) and tid.strip():
                task_ids.append(tid)
        if not task_ids:
            return []

        try:
            conn = _kb.connect(board=board_slug)
        except Exception as exc:
            logger.debug(
                "kanban orchestrator callback: cannot open %s for parent "
                "auto-completion: %s",
                board_slug, exc,
            )
            return []
        try:
            auto_completed = _kb.auto_complete_parents(conn, task_ids)
        except Exception as exc:
            logger.warning(
                "kanban orchestrator callback: auto_complete_parents "
                "failed on board %s: %s",
                board_slug, exc,
            )
            auto_completed = []
        finally:
            conn.close()
        if auto_completed:
            logger.info(
                "kanban orchestrator callback: auto-completed %d parent(s) "
                "on board %s: %s",
                len(auto_completed), board_slug, ", ".join(auto_completed),
            )
        return auto_completed

    def _inject_epoch(self, msg_text: str, slug: str, stats: dict) -> None:
        """Schedule epoch injection into the user's session (fire-and-forget).

        Looks up the last kanban-interaction source for *slug*, builds a
        synthetic ``MessageEvent``, and schedules a background task that
        processes the event through the agent and delivers a combined
        summary + response via the platform adapter.

        ``stats`` is the dict returned by :meth:`_detect_epoch`.
        """
        event_details = stats["event_details"]
        current_epoch = stats["current_epoch"]
        MAX_EPOCHS = stats["MAX_EPOCHS"]
        ready_count = stats["ready_count"]

        try:
            from gateway.config import Platform as _Platform
            from gateway.platforms.base import MessageEvent, SessionSource

            _src_map = getattr(self, "_kanban_last_user_source", {})
            last_src = _src_map.get(slug)

            if not last_src or not last_src[0]:
                # Unify with notifier inject: prefer persistent board owner
                # table over in-memory cache to avoid routing to a different
                # session than the notifier would pick.
                try:
                    from hermes_cli import kanban_db as _kb
                    _owner = self._kanban_lookup_board_owner(slug, db_mod=_kb)
                    if _owner and _owner[0]:
                        last_src = _owner
                except Exception:
                    pass

            if not last_src or not last_src[0]:
                logger.debug(
                    "kanban orchestrator callback: no source for board %s, skipping",
                    slug,
                )
                return

            _plat_str, _chat_id = last_src
            try:
                epoch_platform = _Platform(_plat_str)
            except ValueError:
                logger.warning(
                    "kanban orchestrator callback: invalid platform %s for board %s",
                    _plat_str, slug,
                )
                return

            source = SessionSource(
                platform=epoch_platform,
                chat_id=_chat_id,
                chat_type="private",
                user_id="system",
                user_name="kanban-orchestrator",
            )

            synthetic_event = MessageEvent(
                text=msg_text,
                source=source,
                internal=True,
            )

            logger.info(
                "kanban orchestrator callback: injecting into %s/%s "
                "for board %s (epoch %d/%d)",
                _plat_str, _chat_id, slug, current_epoch, MAX_EPOCHS,
            )

            try:
                # Build user-facing event summary sent alongside the
                # agent response after the epoch is processed.
                _kind_emoji = {
                    "completed": "✅", "blocked": "⚠️",
                    "crashed": "❌", "gave_up": "💀", "timed_out": "⏰",
                }
                summary_parts = [f"🔄 **Epoch #{current_epoch}** `{slug}`"]
                for ed in event_details:
                    emoji = _kind_emoji.get(ed["kind"], "📌")
                    line = f"{emoji} `{ed['task_id']}` {ed['kind']} (@{ed['assignee']})"
                    if ed["summary"]:
                        line += f"\n   {ed['summary'][:200]}"
                    summary_parts.append(line)
                if ready_count > 0:
                    summary_parts.append(f"📋 待处理: {ready_count}")
                summary_parts.append(f"_(epoch {current_epoch}/{MAX_EPOCHS})_")

                adapter = self.adapters.get(epoch_platform)

                # Inject into session WITHOUT blocking the notifier loop.
                # A synchronous await here would queue user messages behind
                # the epoch processing, causing multi-minute delays.
                # fire-and-forget lets the agent handle it asynchronously.

                async def _epoch_inject():
                    """Process epoch injection and send combined response."""
                    try:
                        response_text = await self._handle_message(synthetic_event)
                    except Exception as exc:
                        logger.warning("kanban epoch injection failed: %s", exc)
                        return

                    # Combine summary + agent response into ONE message.
                    summary_parts.append("")
                    if response_text:
                        summary_parts.append(f"📋 **处理结果:**\n{response_text[:500]}")

                    combined_msg = "\n".join(summary_parts)

                    if adapter:
                        send_result = await adapter.send(
                            chat_id=_chat_id, content=combined_msg,
                        )
                        if send_result and getattr(send_result, "success", False):
                            logger.info(
                                "kanban epoch: sent to %s/%s (epoch %d/%d)",
                                _plat_str, _chat_id, current_epoch, MAX_EPOCHS,
                            )
                        else:
                            err = getattr(send_result, "error", "unknown") if send_result else "no result"
                            logger.warning(
                                "kanban epoch: send to %s/%s FAILED: %s",
                                _plat_str, _chat_id, err,
                            )

                # Schedule as a background task — don't block notifier.
                asyncio.ensure_future(_epoch_inject())
                logger.info(
                    "kanban epoch: scheduled injection for %s/%s (epoch %d/%d, fire-and-forget)",
                    _plat_str, _chat_id, current_epoch, MAX_EPOCHS,
                )
            except Exception as orch_exc:
                logger.warning(
                    "kanban orchestrator callback: failed for board %s: %s",
                    slug, orch_exc,
                )
        except Exception as import_exc:
            logger.warning(
                "kanban orchestrator callback: import error for board %s: %s",
                slug, import_exc,
            )

    def _scan_candidate_boards(self, kanban_cfg: dict) -> list[str]:
        """Determine candidate boards for orchestrator epoch checks.

        Honors ``orchestrator_boards`` allowlist (defaulting to ``[]`` on a
        non-list value); otherwise scans ``~/.hermes/kanban/boards/`` for
        directories (skipping ``_``/``.`` prefixed names). The default board
        is always included so the main kanban DB is never skipped.
        """
        from hermes_cli import kanban_db as _kb

        board_allowlist = kanban_cfg.get("orchestrator_boards", [])
        if not isinstance(board_allowlist, list):
            board_allowlist = []

        if board_allowlist:
            return list(board_allowlist)

        import os as _os
        boards_dir = _os.path.expanduser("~/.hermes/kanban/boards")
        candidate_boards: list[str] = []
        if _os.path.isdir(boards_dir):
            for name in sorted(_os.listdir(boards_dir)):
                if name.startswith("_") or name.startswith("."):
                    continue
                if _os.path.isdir(_os.path.join(boards_dir, name)):
                    candidate_boards.append(name)
        # Always include default board (stored in main kanban.db).
        if _kb.DEFAULT_BOARD not in candidate_boards:
            candidate_boards.append(_kb.DEFAULT_BOARD)
        return candidate_boards

    def _rescue_orphans(self, board_slug: str) -> int:
        """Re-parent/detach orphaned todo children of blocked tasks.

        For each blocked task, find todo children stuck because the parent
        never reaches 'done'. Try re-parenting to a done grandparent first
        (preserves context); otherwise detach entirely. Commits and logs.
        Returns the count of orphans processed (re-parented + detached).
        """
        from hermes_cli import kanban_db as _kb

        rescued = 0
        try:
            conn = _kb.connect(board=board_slug)
            try:
                blocked_tasks = _kb.list_tasks(conn, status="blocked") or []
                for bt in blocked_tasks:
                    # Find todo children of this blocked task.
                    orphan_rows = conn.execute(
                        "SELECT t.id FROM tasks t "
                        "JOIN task_edges e ON e.child_id = t.id "
                        "WHERE e.parent_id = ? AND t.status = 'todo'",
                        (bt.id,),
                    ).fetchall()
                    for orow in orphan_rows:
                        orphan_id = orow[0]
                        # Try re-parenting to grandparent (done tasks only).
                        gp_row = conn.execute(
                            "SELECT e2.parent_id, t2.status FROM task_edges e "
                            "JOIN task_edges e2 ON e2.child_id = e.parent_id "
                            "JOIN tasks t2 ON t2.id = e2.parent_id "
                            "WHERE e.child_id = ?",
                            (orphan_id,),
                        ).fetchone()
                        if gp_row and gp_row[0] and gp_row[1] == "done":
                            # Grandparent exists and is done — re-parent.
                            conn.execute(
                                "DELETE FROM task_edges WHERE parent_id=? AND child_id=?",
                                (bt.id, orphan_id),
                            )
                            conn.execute(
                                "INSERT OR IGNORE INTO task_edges(parent_id, child_id) VALUES(?,?)",
                                (gp_row[0], orphan_id),
                            )
                            logger.info(
                                "kanban epoch: re-parented orphan %s "
                                "from blocked %s to grandparent %s",
                                orphan_id, bt.id, gp_row[0],
                            )
                        else:
                            # No suitable grandparent — detach entirely.
                            conn.execute(
                                "DELETE FROM task_edges WHERE parent_id=? AND child_id=?",
                                (bt.id, orphan_id),
                            )
                            logger.info(
                                "kanban epoch: detached orphan %s from blocked %s",
                                orphan_id, bt.id,
                            )
                        rescued += 1
                conn.commit()
            finally:
                conn.close()
        except Exception as rescue_exc:
            logger.debug("kanban epoch: orphan rescue failed: %s", rescue_exc)
        return rescued

    def _detect_epoch(
        self, board_slug: str, last_eid: int, *, now: float, max_epochs: int,
    ) -> dict | None:
        """Per-board epoch detection + bookkeeping.

        Queries running/ready/blocked tasks and recent terminal events,
        decides whether an epoch should fire, and (when it does) resets the
        stale counter, runs orphan rescue, advances the per-board epoch
        counter, and records cooldown / last-event-id state.

        Returns a stats dict on trigger, or None when the board is skipped
        (idle, no terminal events, over the epoch limit, or a DB error) —
        mirroring the original per-board ``continue`` skips.
        """
        from hermes_cli import kanban_db as _kb

        # Count in-progress and ready tasks on this board.
        try:
            conn = _kb.connect(board=board_slug)
            try:
                tasks = _kb.list_tasks(conn, status="running")
                in_progress_count = len(tasks) if tasks else 0
                ready_tasks = _kb.list_tasks(conn, status="ready")
                ready_count = len(ready_tasks) if ready_tasks else 0
                # Check for recent terminal events since last epoch trigger.
                # Uses per-board last_event_id to avoid re-triggering on
                # old events. Falls back to 600s window on first run.
                if last_eid > 0:
                    recent_event_rows = conn.execute(
                        "SELECT te.id, te.task_id, te.kind, te.payload "
                        "FROM task_events te WHERE te.id > ? "
                        "AND te.kind IN ('completed','blocked','crashed','gave_up','timed_out') "
                        "ORDER BY te.id DESC LIMIT 20",
                        (last_eid,),
                    ).fetchall()
                else:
                    cutoff = time.time() - 600
                    recent_event_rows = conn.execute(
                        "SELECT te.id, te.task_id, te.kind, te.payload "
                        "FROM task_events te WHERE te.created_at > ? "
                        "AND te.kind IN ('completed','blocked','crashed','gave_up','timed_out') "
                        "ORDER BY te.created_at DESC LIMIT 20",
                        (cutoff,),
                    ).fetchall()
                recent_events = [(r[2],) for r in recent_event_rows]  # kind-only for Counter compat
                # Build detailed event list for user-facing summary.
                event_details = []
                for r in recent_event_rows:
                    _eid, _tid, _kind, _payload = r
                    # Get task info
                    _tinfo = conn.execute(
                        "SELECT title, assignee, result FROM tasks WHERE id=?", (_tid,)
                    ).fetchone()
                    _title = _tinfo[0] if _tinfo else "?"
                    _assignee = _tinfo[1] if _tinfo else "?"
                    # Extract summary from payload or task result
                    _summary = ""
                    if _payload:
                        try:
                            _p = json.loads(_payload)
                            _summary = _p.get("summary", "")
                        except Exception:
                            _summary = ""
                    if not _summary and _tinfo and _tinfo[2]:
                        _summary = _tinfo[2][:200]
                    # Extract reason for blocked events (used by auto-handling)
                    _reason = ""
                    if _payload:
                        try:
                            _p = json.loads(_payload)
                            _reason = _p.get("reason", "")
                        except Exception:
                            _reason = ""
                    event_details.append({
                        "task_id": _tid,
                        "kind": _kind,
                        "title": _title,
                        "assignee": _assignee,
                        "summary": _summary,
                        "reason": _reason,
                    })
                any_terminal = len(recent_events) > 0
                # Query blocked count here while conn is still open.
                blocked_count = len(_kb.list_tasks(conn, status="blocked") or [])
            finally:
                conn.close()
        except Exception as exc:
            logger.debug(
                "kanban orchestrator callback: board %s check failed: %s",
                board_slug, exc,
            )
            return None

        in_progress_names = [t.id for t in (tasks or [])]

        # Trigger epoch when there are terminal events AND no ready tasks.
        # We allow triggering even with running tasks, because a running
        # task might be an auto-re-dispatch from a crash — the orchestrator
        # needs to know about the crash to decide next steps.
        if not any_terminal and ready_count == 0 and in_progress_count > 0:
            return None

        # A board with 0 running tasks but also 0 ready and no recent
        # terminal events is simply idle — skip without counting as
        # stale. Only trigger epoch when there's actually something
        # to act on (ready tasks to dispatch or terminal events to
        # react to).
        if not ready_count and not any_terminal:
            return None
        # Reset stale counter — something real happened.
        self._orch_cb_stale_counts[board_slug] = 0

        # Rescue orphaned children (re-parent/detach blocked-task todo kids).
        self._rescue_orphans(board_slug)

        # Epoch counter tracks active work on this board. Reset when:
        # 1. Board is idle (no running/ready/blocked = workflow finished)
        # 2. New terminal events arrived (fresh epoch budget per new event)
        is_idle = (in_progress_count == 0 and ready_count == 0 and blocked_count == 0)
        if is_idle or any_terminal:
            self._orch_epoch_counts[board_slug] = 0

        # Anti-loop: max epoch limit per "wave" — between terminal events,
        # cap orchestrator re-dispatch attempts to avoid hot-looping.
        current_epoch = self._orch_epoch_counts.get(board_slug, 0) + 1
        if current_epoch > max_epochs:
            logger.info(
                "kanban orchestrator callback: board %s epoch limit (%d/%d); "
                "waiting for next terminal event or new task",
                board_slug, current_epoch - 1, max_epochs,
            )
            return None
        self._orch_epoch_counts[board_slug] = current_epoch

        self._orch_cb_cooldowns[board_slug] = now

        # Record the max event id for this board so we only trigger on
        # NEW terminal events next time.
        try:
            conn2 = _kb.connect(board=board_slug)
            try:
                max_eid_row = conn2.execute("SELECT MAX(id) FROM task_events").fetchone()
                if max_eid_row and max_eid_row[0]:
                    self._orch_cb_last_event_id[board_slug] = max_eid_row[0]
            finally:
                conn2.close()
        except Exception as eid_exc:
            logger.debug(
                "kanban orchestrator callback: max event id update failed for %s: %s",
                board_slug, eid_exc,
            )

        return {
            "in_progress_count": in_progress_count,
            "in_progress_names": in_progress_names,
            "ready_count": ready_count,
            "blocked_count": blocked_count,
            "event_details": event_details,
            "has_terminal_events": any_terminal,
            "current_epoch": current_epoch,
            "MAX_EPOCHS": max_epochs,
            "max_eid": self._orch_cb_last_event_id.get(board_slug, last_eid),
        }

    def _has_force_failure(
        self, board_slug: str, threshold: int,
    ) -> bool:
        """Return True if any task on the board has hit the failure threshold.

        ``consecutive_failures`` is incremented by ``_record_failure`` on
        every spawn/timeout crash path, and cleared on successful
        completion. A value at or above ``threshold`` (default 3) is the
        Phase 2 rule 3 signal: the worker keeps failing on the same task
        with no progress, so the orchestrator must step in even when
        other ready cards are waiting.

        Reads ``tasks`` only (no event log join) — the column is kept
        current by the failure paths in
        :func:`hermes_cli.kanban_db._record_failure`. Returns False on
        any DB error so a transient failure does not falsely force a
        orchestrator injection.
        """
        from hermes_cli import kanban_db as _kb

        try:
            conn = _kb.connect(board=board_slug)
        except Exception as exc:
            logger.debug(
                "kanban orchestrator callback: cannot open %s to check "
                "force-failure threshold: %s",
                board_slug, exc,
            )
            return False
        try:
            row = conn.execute(
                "SELECT 1 FROM tasks "
                "WHERE consecutive_failures >= ? "
                "AND status IN ('ready','blocked','running') "
                "LIMIT 1",
                (int(threshold),),
            ).fetchone()
        except Exception as exc:
            logger.debug(
                "kanban orchestrator callback: force-failure check on "
                "board %s failed: %s",
                board_slug, exc,
            )
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return row is not None

    def _failing_task_ids(
        self, board_slug: str, threshold: int,
    ) -> list[str]:
        """Return the ids of tasks currently at/over the failure threshold.

        Companion to :meth:`_has_force_failure`. Used to annotate the
        orchestrator message so the LLM can prioritize the cards that
        actually need triage. Failures here are advisory; a DB error
        yields an empty list rather than raising — the caller treats it
        as "we know something is wrong but we couldn't name it".
        """
        from hermes_cli import kanban_db as _kb

        try:
            conn = _kb.connect(board=board_slug)
        except Exception as exc:
            logger.debug(
                "kanban orchestrator callback: cannot open %s to list "
                "failing task ids: %s",
                board_slug, exc,
            )
            return []
        try:
            rows = conn.execute(
                "SELECT id, consecutive_failures FROM tasks "
                "WHERE consecutive_failures >= ? "
                "AND status IN ('ready','blocked','running') "
                "ORDER BY consecutive_failures DESC, id ASC "
                "LIMIT 20",
                (int(threshold),),
            ).fetchall()
        except Exception as exc:
            logger.debug(
                "kanban orchestrator callback: failing-task-id query on "
                "board %s failed: %s",
                board_slug, exc,
            )
            return []
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return [r[0] for r in rows]

    def _build_epoch_message(
        self, board_slug: str, events: list, stats: dict,
    ) -> str:
        """Build the multi-line orchestrator notification message for one epoch.

        ``events`` is the ``event_details`` list (used to summarize event
        kinds); ``stats`` carries the counts produced by ``_detect_epoch``
        and (when applicable) the ``auto_completed`` list of parents
        this watcher just flipped to ``done`` because all their children
        reached a terminal state. ``stats`` may also carry
        ``force_urgent`` (Phase 2 rule 3) and ``failing_task_ids`` —
        when present the message is prefixed with an ``[URGENT]`` tag
        and a short list of cards that need triage so the orchestrator
        LLM triages them first.
        """
        from hermes_cli import kanban_db as _kb

        current_epoch = stats["current_epoch"]
        MAX_EPOCHS = stats["MAX_EPOCHS"]
        ready_count = stats["ready_count"]
        in_progress_names = stats["in_progress_names"]
        auto_completed = stats.get("auto_completed") or []
        force_urgent = bool(stats.get("force_urgent"))
        failing_ids = stats.get("failing_task_ids") or []
        force_threshold = stats.get("force_failure_threshold")

        # Build the notification message with event summaries.
        board_label = (
            f"board={board_slug}" if board_slug != _kb.DEFAULT_BOARD else "default board"
        )

        # Summarize what happened: count by event kind from recent_events
        event_kinds: Counter = Counter()
        for ed in events:
            event_kinds[ed["kind"]] += 1
        event_summary = ", ".join(
            f"{k}={v}" for k, v in sorted(event_kinds.items())
        ) if event_kinds else "no events"

        # Phase 2 rule 3: tag the message as urgent when at least one
        # card on the board has hit the consecutive-failure threshold.
        # This puts the failing cards at the top of the orchestrator's
        # mental queue even when other ready cards are queued behind.
        urgent_tag = "[URGENT] " if force_urgent else ""

        msg_lines = [
            f"{urgent_tag}[Kanban Epoch #{current_epoch}] Workers idle on {board_label}.",
            f"Events this tick: {event_summary}",
        ]
        if force_urgent:
            threshold_note = (
                f">= {force_threshold} failures"
                if force_threshold is not None
                else "over the failure threshold"
            )
            preview = ", ".join(failing_ids[:8])
            suffix = "" if len(failing_ids) <= 8 else f" (+{len(failing_ids) - 8} more)"
            msg_lines.append(
                f"🚨 Stuck tasks ({threshold_note}): {preview}{suffix}"
            )
        if in_progress_names:
            msg_lines.append(f"Running tasks: {len(in_progress_names)} ({', '.join(in_progress_names[:5])})")
        if auto_completed:
            # Phase-1 auto-completion summary: parents that flipped to
            # done because all their children reached a terminal state.
            # Truncate the id list to keep the message bounded; the
            # full list is on the per-task ``completed`` event payload.
            preview = ", ".join(auto_completed[:8])
            suffix = "" if len(auto_completed) <= 8 else f" (+{len(auto_completed) - 8} more)"
            msg_lines.append(
                f"🤖 Auto-completed parents: {preview}{suffix}"
            )
        if ready_count > 0:
            msg_lines.append(
                f"{ready_count} ready task(s) queued — decompose and dispatch."
            )
        else:
            msg_lines.append(
                "No ready tasks. Review blocked/crashed tasks and re-decompose if needed."
            )
        msg_lines.append(f"(epoch {current_epoch}/{MAX_EPOCHS})")

        # Orchestrator instructions — the LLM receives this as the user message.
        # ── Event-specific action table (auto-closure logic) ──────────
        blocked_events = [ed for ed in events if ed["kind"] == "blocked"]
        crashed_events = [ed for ed in events if ed["kind"] in ("crashed", "gave_up")]
        completed_events = [ed for ed in events if ed["kind"] == "completed"]

        msg_lines.append("")
        msg_lines.append("--- Event Auto-Handling Rules ---")
        msg_lines.append("IMPORTANT: You are the event closer. ACT, don't report.")

        if blocked_events:
            msg_lines.append("")
            msg_lines.append("### Blocked Events — must resolve NOW:")
            for ed in blocked_events:
                reason_hint = f" — {ed.get('reason', '')[:120]}" if ed.get("reason") else ""
                msg_lines.append(f"- `{ed['task_id']}`: {ed['title']}{reason_hint}")
            msg_lines.append("")
            msg_lines.append("For each blocked task, apply the matching rule:")
            msg_lines.append("  • reason starts with 'review-required': read the changes (kanban_show),")
            msg_lines.append("    review the output. If OK → kanban_unblock. If issues → kanban_comment.")
            msg_lines.append("  • reason starts with '❌ 验证失败': kanban_comment the failure reason,")
            msg_lines.append("    then decide — fix & unblock OR re-dispatch to the assignee.")
            msg_lines.append("  • reason starts with '⚠️ 需人工确认': ONLY notify the user, do nothing else.")
            msg_lines.append("  • reason is empty or 'unknown': inspect (kanban_show), determine cause,")
            msg_lines.append("    then unblock or re-dispatch.")

        if crashed_events:
            msg_lines.append("")
            msg_lines.append("### Crashed/Gave-up Events — take over or re-dispatch:")
            for ed in crashed_events:
                msg_lines.append(f"- `{ed['task_id']}`: {ed['title']} (@{ed['assignee']})")
            msg_lines.append("")
            msg_lines.append("These workers FAILED. Do NOT just report the failure.")
            msg_lines.append("  • Simple verification tasks (compile, grep, test < 5min):")
            msg_lines.append("    → DO IT YOURSELF with terminal/search_files, then kanban_complete.")
            msg_lines.append("  • Complex implementation: create a fresh task card, assign appropriately.")
            msg_lines.append("  • Body contained bad instructions (oc.sh, nonexistent commands):")
            msg_lines.append("    → kanban_comment with correct workflow, then kanban_unblock.")

        if completed_events:
            msg_lines.append("")
            msg_lines.append("### Completed Events — chain propagation:")
            msg_lines.append("  • Check if any blocked child task now has all parents done → unblock.")
            msg_lines.append("  • Check if all children of a parent are terminal → complete the parent.")

        # ── General orchestrator instructions ─────────────────────────
        msg_lines.append("")
        msg_lines.append("--- Orchestrator Instructions ---")
        msg_lines.append(f"Board '{board_slug}': {ready_count} ready, {len(in_progress_names)} running")
        if force_urgent:
            msg_lines.append("")
            msg_lines.append("[URGENT] One or more cards have hit the consecutive-failure threshold.")
            msg_lines.append("Triage the stuck tasks first: inspect, re-assign, re-decompose, or unblock.")
            msg_lines.append("These cards will keep failing in a loop until the orchestrator intervenes.")
        msg_lines.append("")
        msg_lines.append("As the kanban orchestrator, respond by EXECUTING tools — not by analyzing in text.")
        msg_lines.append("You MUST make at least one tool call this turn (kanban list/show/create/unblock).")
        msg_lines.append("Your text response will NOT be seen by anyone. Only tool results matter.")
        msg_lines.append("")
        msg_lines.append("Actions to take:")
        msg_lines.append("1. First: resolve ALL blocked/crashed/gave_up events above (see Event Auto-Handling Rules)")
        msg_lines.append("2. Then: if there are ready/pending items, create the next epoch's tasks")
        msg_lines.append("3. Be mindful of budget — don't create too many parallel tasks at once")
        msg_lines.append("4. Only create kanban tasks — the worker system handles execution")
        msg_lines.append("5. Do NOT send messages to the user — results are delivered automatically")
        return "\n".join(msg_lines)

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        For each subscription row, fetches ``task_events`` newer than the
        stored cursor with kind in the terminal set (``completed``,
        ``blocked``, ``gave_up``, ``crashed``, ``timed_out``). Sends one
        message per new event to ``(platform, chat_id, thread_id)``,
        then advances the cursor. When a task reaches a terminal state
        (``completed`` / ``archived``), the subscription is removed.

        Runs in the gateway event loop; all SQLite work is pushed to a
        thread via ``asyncio.to_thread`` so the loop never blocks on the
        WAL lock. Failures in one tick don't stop subsequent ticks.

        **Multi-board:** iterates every board discovered on disk per
        tick. Subscriptions live inside each board's own DB and cannot
        cross boards, so delivery semantics are unchanged — this is
        purely a fan-out of the single-DB poll.
        """
        # Gate: only the dispatch-owning gateway opens kanban DBs for notifier polling.
        # Non-dispatch gateways have no subscriptions to deliver — all kanban state lives
        # in the dispatch owner's per-board DBs. This prevents N-gateway -shm contention.
        # TODO: gate per-board when per-board dispatcher_owner tracking lands.
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban notifier: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban notifier: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban notifier: disabled via config kanban.dispatch_in_gateway=false"
            )
            return
        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        TERMINAL_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")
        # Subscriptions are removed only when the task reaches a truly final
        # status (done / archived). We used to also unsub on any terminal
        # event kind (gave_up / crashed / timed_out / blocked), but that
        # silently dropped the user out of the loop whenever the dispatcher
        # respawned the task: a worker that crashes, gets reclaimed, runs
        # again, and crashes a second time would only notify on the first
        # crash because the subscription was deleted after the first event.
        # Same shape as the reblock-after-unblock cycle that PR #22941
        # fixed for `blocked`. Keeping the subscription alive until the
        # task is genuinely done lets the cursor (advanced atomically by
        # claim_unseen_events_for_sub) handle dedup, and any retry-loop
        # event reaches the user.
        # Per-subscription send-failure counter. Adapter.send raising
        # means the chat is dead (deleted, bot kicked, etc.) — after N
        # consecutive send failures the sub is dropped so we don't spin
        # against a dead chat every 5 seconds forever.
        MAX_SEND_FAILURES = 3
        sub_fail_counts: dict[tuple, int] = getattr(
            self, "_kanban_sub_fail_counts", {}
        )
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None)
        if not notifier_profile:
            notifier_profile = self._active_profile_name()
            self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        while self._running:
            try:
                def _collect():
                    deliveries: list[dict] = []
                    active_platforms = {
                        getattr(platform, "value", str(platform)).lower()
                        for platform in self.adapters.keys()
                    }
                    if not active_platforms:
                        logger.debug("kanban notifier: no connected adapters; skipping tick")
                        return deliveries

                    # Enumerate every board on disk, but poll each resolved DB
                    # path once. Multiple slugs can point at the same DB when
                    # HERMES_KANBAN_DB pins the board path; without this guard
                    # one gateway could collect the same subscription/event
                    # more than once before advancing the cursor.
                    try:
                        boards = _kb.list_boards(include_archived=False)
                    except Exception:
                        boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
                    seen_db_paths: dict[str, set[str]] = {}
                    for board_meta in boards:
                        slug = board_meta.get("slug") or _kb.DEFAULT_BOARD
                        db_path = board_meta.get("db_path")
                        try:
                            resolved_db_path = str(Path(db_path).expanduser().resolve()) if db_path else str(_kb.kanban_db_path(slug).resolve())
                        except Exception:
                            resolved_db_path = f"slug:{slug}"
                        if resolved_db_path in seen_db_paths:
                            # When multiple slugs map to the same DB, we still
                            # need to process events for EACH slug separately
                            # — skip only truly duplicate (db_path, slug) pairs.
                            if slug in seen_db_paths.get(resolved_db_path, set()):
                                logger.debug(
                                    "kanban notifier: skipping duplicate board slug %s for DB %s",
                                    slug, resolved_db_path,
                                )
                                continue
                        seen_db_paths.setdefault(resolved_db_path, set()).add(slug)
                        try:
                            conn = _kb.connect(board=slug)
                        except Exception as exc:
                            logger.debug("kanban notifier: cannot open board %s: %s", slug, exc)
                            continue
                        try:
                            # `connect()` runs the schema + idempotent migration
                            # on first open per process, so an explicit
                            # `init_db()` here would be redundant. Worse:
                            # `init_db()` deliberately busts the per-process
                            # cache and re-runs the migration on a *second*
                            # connection, which races the first and used to
                            # log a benign but noisy `duplicate column name`
                            # traceback (and intermittent "database is locked"
                            # — issue #21378) on every gateway start against
                            # a legacy DB. `_add_column_if_missing` now
                            # tolerates that race, but we still skip the
                            # redundant call to avoid the wasted work.
                            subs = _kb.list_notify_subs(conn)
                            if not subs:
                                logger.debug("kanban notifier: board %s has no subscriptions", slug)
                            for sub in subs:
                                owner_profile = sub.get("notifier_profile") or None
                                if owner_profile and owner_profile != notifier_profile:
                                    logger.debug(
                                        "kanban notifier: subscription for %s owned by profile %s; current profile %s skipping",
                                        sub.get("task_id"), owner_profile, notifier_profile,
                                    )
                                    continue
                                platform = (sub.get("platform") or "").lower()
                                if platform not in active_platforms:
                                    logger.debug(
                                        "kanban notifier: subscription for %s on %s skipped; adapter not connected",
                                        sub.get("task_id"), platform or "<missing>",
                                    )
                                    continue
                                old_cursor, cursor, events = _kb.claim_unseen_events_for_sub(
                                    conn,
                                    task_id=sub["task_id"],
                                    platform=sub["platform"],
                                    chat_id=sub["chat_id"],
                                    thread_id=sub.get("thread_id") or "",
                                    kinds=TERMINAL_KINDS,
                                )
                                if not events:
                                    continue
                                task = _kb.get_task(conn, sub["task_id"])
                                logger.debug(
                                    "kanban notifier: claimed %d event(s) for %s on board %s cursor %s→%s",
                                    len(events), sub["task_id"], slug, old_cursor, cursor,
                                )
                                deliveries.append({
                                    "sub": sub,
                                    "old_cursor": old_cursor,
                                    "cursor": cursor,
                                    "events": events,
                                    "task": task,
                                    "board": slug,
                                })
                        finally:
                            conn.close()
                    return deliveries

                deliveries = await asyncio.to_thread(_collect)
                for d in deliveries:
                    sub = d["sub"]
                    task = d["task"]
                    board_slug = d.get("board")
                    platform_str = (sub["platform"] or "").lower()
                    try:
                        plat = _Platform(platform_str)
                    except ValueError:
                        # Unknown platform string; skip and advance cursor so
                        # we don't replay forever.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        continue
                    adapter = self.adapters.get(plat)
                    if adapter is None:
                        logger.debug(
                            "kanban notifier: adapter %s disconnected before delivery for %s; rewinding claim",
                            platform_str, sub["task_id"],
                        )
                        await asyncio.to_thread(
                            self._kanban_rewind,
                            sub,
                            d["cursor"],
                            d.get("old_cursor", 0),
                            board_slug,
                        )
                        continue
                    title = (task.title if task else sub["task_id"])[:120]
                    for ev in d["events"]:
                        kind = ev.kind
                        # Identity prefix: attribute terminal pings to the
                        # worker that did the work. Makes fleets (where one
                        # chat subscribes to many tasks) legible at a glance.
                        who = (task.assignee if task and task.assignee else None)
                        tag = f"@{who} " if who else ""
                        if kind == "completed":
                            # Prefer the run's summary (the worker's
                            # intentional human-facing handoff, carried
                            # in the event payload), then fall back to
                            # task.result for legacy rows written before
                            # runs shipped.
                            handoff = ""
                            payload_summary = None
                            if ev.payload and ev.payload.get("summary"):
                                payload_summary = str(ev.payload["summary"])
                            if payload_summary:
                                lines = payload_summary.strip().splitlines()
                                h = lines[0][:200] if lines else payload_summary[:200]
                                handoff = f"\n{h}"
                            elif task and task.result:
                                lines = task.result.strip().splitlines()
                                r = lines[0][:160] if lines else task.result[:160]
                                handoff = f"\n{r}"
                            msg = (
                                f"✔ {tag}Kanban {sub['task_id']} done"
                                f" — {title}{handoff}"
                            )
                        elif kind == "blocked":
                            reason = ""
                            if ev.payload and ev.payload.get("reason"):
                                reason = f": {str(ev.payload['reason'])[:160]}"
                            msg = f"⏸ {tag}Kanban {sub['task_id']} blocked{reason}"
                        elif kind == "gave_up":
                            err = ""
                            if ev.payload and ev.payload.get("error"):
                                err = f"\n{str(ev.payload['error'])[:200]}"
                            msg = (
                                f"✖ {tag}Kanban {sub['task_id']} gave up "
                                f"after repeated spawn failures{err}"
                            )
                        elif kind == "crashed":
                            msg = (
                                f"✖ {tag}Kanban {sub['task_id']} worker crashed "
                                f"(pid gone); dispatcher will retry"
                            )
                        elif kind == "timed_out":
                            limit = 0
                            if ev.payload and ev.payload.get("limit_seconds"):
                                limit = int(ev.payload["limit_seconds"])
                            msg = (
                                f"⏱ {tag}Kanban {sub['task_id']} timed out "
                                f"(max_runtime={limit}s); will retry"
                            )
                        else:
                            continue
                        metadata: dict[str, Any] = {}
                        if sub.get("thread_id"):
                            metadata["thread_id"] = sub["thread_id"]
                        sub_key = (
                            sub["task_id"], sub["platform"],
                            sub["chat_id"], sub.get("thread_id") or "",
                        )
                        try:
                            await adapter.send(
                                sub["chat_id"], msg, metadata=metadata,
                            )
                            logger.debug(
                                "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                                kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                            )
                            # After delivering the text notification, surface
                            # any artifact paths the worker referenced in
                            # ``kanban_complete(summary=..., artifacts=[...])``
                            # (or the legacy ``result`` field) as native
                            # uploads. ``extract_local_files`` finds bare
                            # absolute paths in the summary;
                            # ``send_document`` / ``send_image_file`` uploads
                            # them. Only fires on the ``completed`` event so
                            # we never spam attachments on retries.
                            if kind == "completed":
                                try:
                                    await self._deliver_kanban_artifacts(
                                        adapter=adapter,
                                        chat_id=sub["chat_id"],
                                        metadata=metadata,
                                        event_payload=getattr(ev, "payload", None),
                                        task=task,
                                    )
                                except Exception as art_exc:
                                    logger.debug(
                                        "kanban notifier: artifact delivery for %s failed: %s",
                                        sub["task_id"], art_exc,
                                    )
                            # Reset the failure counter on success.
                            sub_fail_counts.pop(sub_key, None)

                            # After delivering the text notification, optionally
                            # inject the event into the main agent session for
                            # that board (per-event real-time awareness).
                            # Fire-and-forget so the notifier loop is never
                            # blocked.
                            if self._kanban_notifier_inject_enabled(kanban_cfg):
                                asyncio.ensure_future(
                                    self._kanban_inject_event(
                                        event=ev,
                                        task=task,
                                        board_slug=board_slug,
                                        sub=sub,
                                    )
                                )
                        except Exception as exc:
                            fails = sub_fail_counts.get(sub_key, 0) + 1
                            sub_fail_counts[sub_key] = fails
                            logger.warning(
                                "kanban notifier: send failed for %s on %s "
                                "(attempt %d/%d): %s",
                                sub["task_id"], platform_str, fails,
                                MAX_SEND_FAILURES, exc,
                            )
                            if fails >= MAX_SEND_FAILURES:
                                logger.warning(
                                    "kanban notifier: dropping subscription "
                                    "%s on %s after %d consecutive send failures",
                                    sub["task_id"], platform_str, fails,
                                )
                                await asyncio.to_thread(self._kanban_unsub, sub, board_slug)
                                sub_fail_counts.pop(sub_key, None)
                            else:
                                await asyncio.to_thread(
                                    self._kanban_rewind,
                                    sub,
                                    d["cursor"],
                                    d.get("old_cursor", 0),
                                    board_slug,
                                )
                            # Rewind the pre-send claim on transient failure so
                            # a later tick can retry. After too many failures,
                            # dropping the subscription is the terminal action.
                            break
                    else:
                        # All events delivered; advance cursor. The cursor
                        # is the dedup mechanism — it prevents re-delivery
                        # of the same event on subsequent ticks.
                        await asyncio.to_thread(
                            self._kanban_advance, sub, d["cursor"], board_slug,
                        )
                        # Unsubscribe only when the task has reached a truly
                        # final status (done / archived). For blocked /
                        # gave_up / crashed / timed_out the subscription is
                        # kept alive so the user gets notified again if the
                        # dispatcher respawns the task and it cycles into the
                        # same state. See the longer comment on TERMINAL_KINDS
                        # above for the failure mode this prevents.
                        task_terminal = task and task.status in {"done", "archived"}
                        if task_terminal:
                            await asyncio.to_thread(
                                self._kanban_unsub, sub, board_slug,
                            )
                # ── Orchestrator epoch callback ────────────────────────
                # Runs once per tick, independent of whether there were
                # any deliveries. Fire-and-forget so it doesn't block
                # the notifier loop (and thus user message delivery).
                if kanban_cfg.get("orchestrator_notify"):
                    asyncio.ensure_future(
                        self._kanban_orchestrator_callback(deliveries, kanban_cfg)
                    )

            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            # Sleep with cancellation checks.
            for _ in range(int(max(1, interval))):
                if not self._running:
                    return
                await asyncio.sleep(1)

    def _kanban_notifier_inject_enabled(self, kanban_cfg: dict) -> bool:
        return bool(kanban_cfg.get("notifier_inject", False))

    async def _kanban_inject_event(
        self,
        *,
        event,
        task,
        board_slug: str,
        sub: dict,
    ) -> None:
        """Fire-and-forget: inject a kanban terminal event into a
        dedicated **system session** (like cron jobs do).

        The synthetic event is marked ``system_session=True`` so the
        agent processes it and any output is delivered through the
        kanban notifier's *own* delivery channel — never through the
        user session's reply pipeline. The user does not receive the
        agent's internal reasoning.

        The board owner (the platform/chat that last operated on the
        board) is looked up from the persistent ``kanban_board_owners``
        table. Falls back to the in-memory cache for back-compat and to
        the subscription's own (platform, chat_id) if neither has a
        record — that way a freshly-subscribed user still gets the
        event injected into their own session as a one-shot bootstrap.
        """
        try:
            from hermes_cli import kanban_db as _kb
            try:
                from gateway.config import Platform as _Platform
            except ImportError:
                return

            owner_source = self._kanban_lookup_board_owner(
                board_slug, fallback_sub=sub, db_mod=_kb,
            )
            if not owner_source or not owner_source[0]:
                logger.debug(
                    "kanban notifier inject: no source for board %s, skipping",
                    board_slug,
                )
                return
            _plat_str, _chat_id = owner_source
            try:
                plat = _Platform(_plat_str)
            except ValueError:
                return
            from gateway.session import SessionSource
            from gateway.platforms.base import MessageEvent
            source = SessionSource(
                platform=plat,
                chat_id=_chat_id,
                chat_type="private",
                user_id="system",
                user_name="kanban-notifier",
            )
            kind = event.kind
            task_id = sub["task_id"]
            assignee = (
                task.assignee
                if task and getattr(task, "assignee", None)
                else "unknown"
            )
            title = (
                task.title if task and getattr(task, "title", None) else task_id
            )[:120]
            detail = ""
            if kind == "completed":
                payload_summary = (event.payload or {}).get("summary", "")
                if payload_summary:
                    detail = str(payload_summary).strip().splitlines()[0][:200]
                elif task and getattr(task, "result", None):
                    detail = str(task.result).strip().splitlines()[0][:200]
            elif kind == "blocked":
                detail = str((event.payload or {}).get("reason", ""))[:200]
            elif kind == "gave_up":
                detail = str((event.payload or {}).get("error", ""))[:200]
            elif kind == "crashed":
                detail = "worker crashed (pid gone); dispatcher will retry"
            elif kind == "timed_out":
                limit = (event.payload or {}).get("limit_seconds", 0)
                detail = f"timed out (max_runtime={limit}s); will retry"
            event_text = (
                f"[KANBAN-EVENT] {kind} | task: {task_id} | "
                f"board: {board_slug} | assignee: {assignee}\n\n"
                f"## {kind.upper()} — {title}\n\n"
                f"{detail}\n\n"
                f"---\n"
                f"metadata: task_id={task_id}, kind={kind}, "
                f"board={board_slug}, assignee={assignee}"
            )
            # system_session=True routes this through the system-session
            # pipeline: the agent runs, but the response is delivered
            # via this mixin's own delivery channel rather than the
            # user-facing reply pipeline. See MessageEvent.system_session.
            synthetic_event = MessageEvent(
                text=event_text,
                source=source,
                internal=True,
                system_session=True,
            )
            await self._handle_message(synthetic_event)
            logger.info(
                "kanban notifier inject: delivered %s for %s to %s/%s",
                kind, task_id, _plat_str, _chat_id,
            )
        except Exception as exc:
            logger.warning(
                "kanban notifier inject failed for %s: %s",
                sub.get("task_id", "?"), exc,
            )

    def _kanban_lookup_board_owner(
        self,
        board_slug: str,
        *,
        fallback_sub: dict | None = None,
        db_mod=None,
    ) -> tuple[str, str] | None:
        """Resolve which (platform, chat_id) should receive a kanban
        notifier injection for *board_slug*.

        Lookup order:
          1. Persistent ``kanban_board_owners`` table — survives
             restarts and works across multiple gateway processes.
          2. The in-memory ``_kanban_last_user_source`` cache (set
             during normal handle_message) — covers boards that have
             never been written to the persistent table.
          3. The subscription's own (platform, chat_id) as a last
             resort — ensures a freshly-subscribed user still gets
             events routed somewhere sensible on first delivery.

        Returns ``None`` only when the board has no subscription AND
        no recorded owner. ``db_mod`` is the ``hermes_cli.kanban_db``
        module; defaults to a lazy import when not provided (kept as
        a parameter so tests can stub it).
        """
        if db_mod is None:
            try:
                from hermes_cli import kanban_db as _db
            except ImportError:
                _db = None
            db_mod = _db

        # 1. Persistent owner table.
        if db_mod is not None and hasattr(db_mod, "get_board_owner"):
            try:
                owner = db_mod.get_board_owner(
                    db_mod.connect(board=board_slug), board=board_slug,
                )
                if owner and owner[0] and owner[1]:
                    return owner
            except Exception as exc:
                logger.debug(
                    "kanban board owner lookup on %s failed: %s",
                    board_slug, exc,
                )

        # 2. In-memory cache (legacy path; covers board owners that
        # haven't been flushed to the persistent table yet).
        mem_cache = getattr(self, "_kanban_last_user_source", {}) or {}
        cached = mem_cache.get(board_slug)
        if cached and cached[0]:
            return cached

        # 3. Last-resort fallback: the subscription itself.
        if fallback_sub:
            plat = fallback_sub.get("platform")
            chat = fallback_sub.get("chat_id")
            if plat and chat:
                return (plat, chat)

        return None

    def _kanban_advance(
        self, sub: dict, cursor: int, board: Optional[str] = None,
    ) -> None:
        """Sync helper: advance a subscription's cursor. Runs in to_thread.

        ``board`` scopes the DB connection to the board that owns this
        subscription. Unsub cursors in one board can't touch another's.
        """
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.advance_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                new_cursor=cursor,
            )
        finally:
            conn.close()

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.remove_notify_sub(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
            )
        finally:
            conn.close()

    def _kanban_rewind(
        self,
        sub: dict,
        claimed_cursor: int,
        old_cursor: int,
        board: Optional[str] = None,
    ) -> None:
        """Sync helper: undo a claimed notification cursor after send failure."""
        from hermes_cli import kanban_db as _kb
        conn = _kb.connect(board=board)
        try:
            _kb.rewind_notify_cursor(
                conn,
                task_id=sub["task_id"],
                platform=sub["platform"],
                chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "",
                claimed_cursor=claimed_cursor,
                old_cursor=old_cursor,
            )
        finally:
            conn.close()

    async def _deliver_kanban_artifacts(
        self,
        *,
        adapter,
        chat_id: str,
        metadata: dict,
        event_payload: Optional[dict],
        task,
    ) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Workers passing ``kanban_complete(artifacts=[...])`` ship absolute
        file paths through the completion event so downstream humans get
        the deliverable as a native upload instead of a path printed in
        chat.

        Sources scanned, in priority order:
          1. ``event_payload['artifacts']`` (explicit list — preferred)
          2. ``event_payload['summary']`` (truncated first line)
          3. ``task.result`` (legacy fallback)

        Files are deduplicated, missing files are silently skipped (the
        path may have been mentioned for reference only), and delivery
        errors are logged but do not break the notifier loop.
        """
        from pathlib import Path as _Path

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            if not path:
                return
            expanded = os.path.expanduser(path)
            if expanded in seen:
                return
            if not os.path.isfile(expanded):
                return
            seen.add(expanded)
            candidates.append(expanded)

        # 1. Explicit artifacts list in payload.
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                for item in raw:
                    if isinstance(item, str):
                        _add(item)

            # 2. Paths embedded in the payload summary.
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                paths, _ = adapter.extract_local_files(summary)
                for p in paths:
                    _add(p)

        # 3. Legacy: paths embedded in task.result.
        if task is not None and getattr(task, "result", None):
            result_text = str(task.result)
            paths, _ = adapter.extract_local_files(result_text)
            for p in paths:
                _add(p)

        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

        from urllib.parse import quote as _quote

        # Partition images so they ride a single send_multiple_images call
        # on platforms that support batch image uploads (Signal/Slack RPCs).
        image_paths = [p for p in candidates if _Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if _Path(p).suffix.lower() not in _IMAGE_EXTS]

        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(
                    chat_id=chat_id, images=batch, metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: image batch upload failed: %s", exc,
                )

        for path in other_paths:
            ext = _Path(path).suffix.lower()
            try:
                if ext in _VIDEO_EXTS:
                    await adapter.send_video(
                        chat_id=chat_id, video_path=path, metadata=metadata,
                    )
                else:
                    await adapter.send_document(
                        chat_id=chat_id, file_path=path, metadata=metadata,
                    )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: artifact upload (%s) failed: %s",
                    path, exc,
                )

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` in config.yaml (default True).
        When true, the gateway hosts the single dispatcher for this profile:
        no separate `hermes kanban daemon` process needed. When false, the
        loop exits immediately and an external daemon is expected.

        Each tick calls :func:`kanban_db.dispatch_once` inside
        ``asyncio.to_thread`` so the SQLite WAL lock never blocks the
        event loop. Failures in one tick don't stop subsequent ticks —
        same pattern as `_kanban_notifier_watcher`.

        Shutdown: the loop checks ``self._running`` between ticks; gateway
        stop() flips it to False and cancels pending tasks, and the
        in-flight ``to_thread`` returns on its own after the current
        ``dispatch_once`` call finishes (typically <1ms on an idle board).
        """
        # Read config once at boot. If the user flips the flag later, they
        # restart the gateway; same pattern as every other background
        # watcher here. Honours HERMES_KANBAN_DISPATCH_IN_GATEWAY env var
        # as an escape hatch (false-y value disables without editing YAML).
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return

        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info(
                "kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false"
            )
            return

        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return

        try:
            interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
        except (ValueError, TypeError):
            logger.warning(
                "kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                kanban_cfg.get("dispatch_interval_seconds"),
            )
            interval = 60.0
        interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

        # Read max_spawn config to limit concurrent kanban tasks
        max_spawn = kanban_cfg.get("max_spawn", None)
        if max_spawn is not None:
            logger.info(f"kanban dispatcher: max_spawn={max_spawn}")

        # Cap the number of simultaneously running tasks so slow workers
        # (local LLMs, resource-constrained hosts) don't pile up and time
        # out. When set, the dispatcher skips spawning when the board
        # already has this many tasks in 'running' status.
        raw_max_in_progress = kanban_cfg.get("max_in_progress", None)
        max_in_progress = None
        if raw_max_in_progress is not None:
            try:
                max_in_progress = int(raw_max_in_progress)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress=%r; ignoring",
                    raw_max_in_progress,
                )
                max_in_progress = None
            else:
                if max_in_progress < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress=%r is below 1; ignoring",
                        raw_max_in_progress,
                    )
                    max_in_progress = None
                else:
                    logger.info(f"kanban dispatcher: max_in_progress={max_in_progress}")

        raw_failure_limit = kanban_cfg.get("failure_limit", _kb.DEFAULT_FAILURE_LIMIT)
        try:
            failure_limit = int(raw_failure_limit)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT
        if failure_limit < 1:
            logger.warning(
                "kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                raw_failure_limit,
                _kb.DEFAULT_FAILURE_LIMIT,
            )
            failure_limit = _kb.DEFAULT_FAILURE_LIMIT

        # Read stale_timeout_seconds — 0 disables stale detection.
        raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
        try:
            stale_timeout_seconds = int(raw_stale or 0)
        except (TypeError, ValueError):
            logger.warning(
                "kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                "disabling stale detection",
                raw_stale,
            )
            stale_timeout_seconds = 0

        # Read kanban.default_assignee — fallback profile for tasks
        # created without an explicit assignee (e.g. via the dashboard).
        # When set, the dispatcher applies it to unassigned ready tasks
        # instead of skipping them indefinitely (#27145). Empty string
        # (the schema default) means "no fallback, keep skipping" —
        # backward-compatible with existing installs.
        default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
        if default_assignee:
            logger.info(
                "kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                "will route to this profile)",
                default_assignee,
            )

        # Read kanban.max_in_progress_per_profile — per-profile concurrency
        # cap (#21582). When set, no single profile gets more than N
        # workers running at once, even if the global max_in_progress
        # would allow it. Prevents one profile's local model / API quota
        # / browser pool from being overwhelmed by a fan-out.
        raw_per_profile = kanban_cfg.get("max_in_progress_per_profile", None)
        max_in_progress_per_profile = None
        if raw_per_profile is not None:
            try:
                max_in_progress_per_profile = int(raw_per_profile)
            except (TypeError, ValueError):
                logger.warning(
                    "kanban dispatcher: invalid kanban.max_in_progress_per_profile=%r; ignoring",
                    raw_per_profile,
                )
                max_in_progress_per_profile = None
            else:
                if max_in_progress_per_profile < 1:
                    logger.warning(
                        "kanban dispatcher: kanban.max_in_progress_per_profile=%r is below 1; ignoring",
                        raw_per_profile,
                    )
                    max_in_progress_per_profile = None
                else:
                    logger.info(
                        "kanban dispatcher: max_in_progress_per_profile=%d",
                        max_in_progress_per_profile,
                    )

        # Initial delay so the gateway finishes wiring adapters before the
        # dispatcher spawns workers (those workers may hit gateway notify
        # subscriptions etc.). Matches the notifier watcher's delay.
        await asyncio.sleep(5)

        # Health telemetry mirrored from `_cmd_daemon`: warn when ready
        # queue is non-empty but spawns are 0 for N consecutive ticks —
        # usually means broken PATH, missing venv, or credential loss.
        HEALTH_WINDOW = 6
        bad_ticks = 0
        last_warn_at = 0
        # Avoid hot-looping corrupt-looking board DBs, but do not suppress
        # same-fingerprint retries forever: transient WAL/open races can
        # surface as "database disk image is malformed" for one tick.
        CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300
        disabled_corrupt_boards: dict[
            str, tuple[tuple[str, int | None, int | None], float]
        ] = {}

        def _board_db_fingerprint(slug: str) -> tuple[str, int | None, int | None]:
            path = _kb.kanban_db_path(slug)
            try:
                resolved = str(path.expanduser().resolve())
            except Exception:
                resolved = str(path)
            try:
                stat = path.stat()
            except OSError:
                return (resolved, None, None)
            return (resolved, stat.st_mtime_ns, stat.st_size)

        def _is_corrupt_board_db_error(exc: Exception) -> bool:
            corrupt_guard_error = getattr(_kb, "KanbanDbCorruptError", None)
            if corrupt_guard_error is not None and isinstance(exc, corrupt_guard_error):
                return True
            if not isinstance(exc, sqlite3.DatabaseError):
                return False
            msg = str(exc).lower()
            return (
                "file is not a database" in msg
                or "database disk image is malformed" in msg
            )

        def _tick_once_for_board(slug: str) -> "Optional[object]":
            """Run one dispatch_once for a specific board.

            Runs in a worker thread via `asyncio.to_thread`. `board=slug`
            is passed through `dispatch_once` so `resolve_workspace` and
            `_default_spawn` see the right paths. The per-board DB is
            opened explicitly so concurrent boards never share a
            connection handle or accidentally claim across each other.
            """
            conn = None
            fingerprint = _board_db_fingerprint(slug)
            disabled_entry = disabled_corrupt_boards.get(slug)
            if disabled_entry is not None:
                disabled_fingerprint, disabled_at = disabled_entry
                age = time.monotonic() - disabled_at
                if (
                    disabled_fingerprint == fingerprint
                    and age < CORRUPT_BOARD_RETRY_AFTER_SECONDS
                ):
                    return None
                if disabled_fingerprint == fingerprint:
                    logger.info(
                        "kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch",
                        slug,
                        age,
                    )
                else:
                    logger.info(
                        "kanban dispatcher: board %s database changed; retrying dispatch",
                        slug,
                    )
                disabled_corrupt_boards.pop(slug, None)
            try:
                conn = _kb.connect(board=slug)
                # `connect()` runs the schema + idempotent migration on
                # first open per process; the previous explicit
                # `init_db()` call here busted the per-process cache and
                # re-ran the migration on a second connection, racing
                # the first. See the matching comment in
                # `_kanban_notifier_watcher` and issue #21378.
                return _kb.dispatch_once(
                    conn,
                    board=slug,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                    stale_timeout_seconds=stale_timeout_seconds,
                    default_assignee=default_assignee,
                    max_in_progress_per_profile=max_in_progress_per_profile,
                )
            except sqlite3.DatabaseError as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            except Exception as exc:
                if _is_corrupt_board_db_error(exc):
                    disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                    logger.error(
                        "kanban dispatcher: board %s database %s is not a valid "
                        "SQLite database; pausing dispatch for this board until "
                        "the file changes, the gateway restarts, or the "
                        "quarantine timer expires. Move or restore the file, "
                        "then run `hermes kanban init` if you need a fresh board.",
                        slug,
                        fingerprint[0],
                    )
                    return None
                logger.exception("kanban dispatcher: tick failed on board %s", slug)
                return None
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

        def _tick_once() -> "list[tuple[str, Optional[object]]]":
            """Run one dispatch_once per board. Returns (slug, result) pairs.

            Enumerating boards on every tick keeps the dispatcher honest
            when users create a new board mid-run: no restart required,
            the next tick picks it up automatically.
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            out: list[tuple[str, "Optional[object]"]] = []
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                out.append((slug, _tick_once_for_board(slug)))
            return out

        def _ready_nonempty() -> bool:
            """Cheap probe: is there at least one ready+assigned+unclaimed
            task on ANY board whose assignee maps to a real Hermes profile
            (i.e. one the dispatcher would actually spawn for)?

            Tasks assigned to control-plane lanes (e.g. ``orion-cc``,
            ``orion-research``) are pulled by terminals via
            ``claim_task`` directly and never spawnable, so a queue full
            of those is "correctly idle", not "stuck". Filtering them out
            here keeps the stuck-warn fire only on real failures (broken
            PATH, missing venv, credential loss for a real Hermes profile).
            """
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                conn = None
                try:
                    conn = _kb.connect(board=slug)
                    if _kb.has_spawnable_ready(conn):
                        return True
                    if _kb.has_spawnable_review(conn):
                        return True
                except Exception:
                    continue
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass
            return False

        # Auto-decompose: turn fresh triage tasks into ready workgraphs
        # before the dispatcher fans out workers. Gated by
        # ``kanban.auto_decompose`` (default True). Capped by
        # ``kanban.auto_decompose_per_tick`` (default 3) so a bulk-load
        # of triage tasks doesn't burst-spend the aux LLM in one tick;
        # remainder defers to subsequent ticks.
        auto_decompose_enabled = bool(kanban_cfg.get("auto_decompose", True))
        try:
            auto_decompose_per_tick = int(
                kanban_cfg.get("auto_decompose_per_tick", 3) or 3
            )
        except (TypeError, ValueError):
            auto_decompose_per_tick = 3
        if auto_decompose_per_tick < 1:
            auto_decompose_per_tick = 1

        def _auto_decompose_tick() -> int:
            """Run the auto-decomposer for up to N triage tasks across all
            boards. Returns the number of triage tasks that were
            successfully decomposed or specified this tick.
            """
            try:
                from hermes_cli import kanban_decompose as _decomp
            except Exception as exc:  # pragma: no cover
                logger.warning(
                    "kanban auto-decompose: import failed (%s); skipping", exc,
                )
                return 0
            try:
                boards = _kb.list_boards(include_archived=False)
            except Exception:
                boards = [_kb.read_board_metadata(_kb.DEFAULT_BOARD)]
            attempted = 0
            successes = 0
            for b in boards:
                slug = b.get("slug") or _kb.DEFAULT_BOARD
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin this board for the duration of the call — same
                # pattern as the dashboard specify endpoint. The
                # decomposer module connects with no board kwarg and
                # relies on the env var.
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug(
                            "kanban auto-decompose: list_triage_ids failed on board %s (%s)",
                            slug, exc,
                        )
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        try:
                            outcome = _decomp.decompose_task(
                                tid, author="auto-decomposer",
                            )
                        except Exception:
                            logger.exception(
                                "kanban auto-decompose: decompose_task crashed on %s",
                                tid,
                            )
                            continue
                        if outcome.ok:
                            successes += 1
                            if outcome.fanout and outcome.child_ids:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → %d children",
                                    slug, tid, len(outcome.child_ids),
                                )
                            else:
                                logger.info(
                                    "kanban auto-decompose [%s]: %s → single task (no fanout)",
                                    slug, tid,
                                )
                        else:
                            # Common no-op reasons (no aux client configured) shouldn't
                            # spam logs every tick. Log at debug.
                            logger.debug(
                                "kanban auto-decompose [%s]: %s skipped: %s",
                                slug, tid, outcome.reason,
                            )
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
            return successes

        logger.info(
            "kanban dispatcher: embedded in gateway (interval=%.1fs)", interval
        )
        while self._running:
            try:
                # Reap zombie children before per-board work so a board DB
                # failure cannot block cleanup of unrelated workers.
                pids = await asyncio.to_thread(_kb.reap_worker_zombies)
                if pids:
                    logger.info(
                        "kanban dispatcher: reaped %d zombie worker(s), pids=%s",
                        len(pids),
                        pids,
                    )
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                if auto_decompose_enabled:
                    await asyncio.to_thread(_auto_decompose_tick)
                results = await asyncio.to_thread(_tick_once)
                any_spawned = False
                for slug, res in (results or []):
                    if res is not None and getattr(res, "spawned", None):
                        any_spawned = True
                        # Quiet by default — only log when something actually
                        # happened, so an idle gateway stays silent.
                        logger.info(
                            "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                            "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                            slug,
                            len(res.spawned),
                            res.reclaimed,
                            len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                            len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                            res.promoted,
                            len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
                        )
                # Health telemetry (aggregate across boards)
                ready_pending = await asyncio.to_thread(_ready_nonempty)
                if ready_pending and not any_spawned:
                    bad_ticks += 1
                else:
                    bad_ticks = 0
                if bad_ticks >= HEALTH_WINDOW:
                    now = int(time.time())
                    if now - last_warn_at >= 300:
                        logger.warning(
                            "kanban dispatcher stuck: ready queue non-empty for "
                            "%d consecutive ticks but 0 workers spawned. Check "
                            "profile health (venv, PATH, credentials) and "
                            "`hermes kanban list --status ready`.",
                            bad_ticks,
                        )
                        last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            # Sleep in 1s slices so shutdown is snappy — otherwise a stop()
            # waits up to `interval` seconds for the current sleep to finish.
            slept = 0.0
            while slept < interval and self._running:
                await asyncio.sleep(min(1.0, interval - slept))
                slept += 1.0
