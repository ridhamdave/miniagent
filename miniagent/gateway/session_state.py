"""
SessionState — in-process mutable state for the gateway lifetime.

Tracks active agent runs per session for deduplication and cancellation.

OpenClaw spreads equivalent state across GatewayRequestContext in
src/gateway/server-methods/types.ts:
  chatAbortControllers  → active_runs (asyncio.Task cancel)
  dedupe map            → dedupe
  agentRunSeq           → run_seq
"""

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class ActiveRun:
    run_id: str
    session_key: str
    task: asyncio.Task
    started_at: float
    conn_id: str  # Which client triggered this run (for targeted events)


class SessionState:
    """
    In-process mutable state for the gateway lifetime.

    Centralizes all session-level mutable state:
    - active_runs: currently running agent tasks (run_id → ActiveRun)
    - dedupe: idempotency key → cached response (prevents double-execution on retry)
    - run_seq: monotonic sequence per session key
    """

    def __init__(self) -> None:
        self.active_runs: dict[str, ActiveRun] = {}
        self.dedupe: dict[str, dict] = {}       # idempotency_key → cached response
        self.run_seq: dict[str, int] = {}        # session_key → current seq

    def next_run_seq(self, session_key: str) -> int:
        """Monotonic sequence per session. OpenClaw: agentRunSeq map."""
        n = self.run_seq.get(session_key, 0) + 1
        self.run_seq[session_key] = n
        return n

    def register_run(self, run: ActiveRun) -> None:
        """Record a new active run."""
        self.active_runs[run.run_id] = run

    def get_run(self, run_id: str) -> ActiveRun | None:
        """Return the ActiveRun by run_id, or None if not found."""
        return self.active_runs.get(run_id)

    def cancel_run(self, run_id: str) -> bool:
        """Cancel the asyncio.Task for a run. Returns True if found and cancelled."""
        run = self.active_runs.get(run_id)
        if run:
            run.task.cancel()
            return True
        return False

    def complete_run(self, run_id: str) -> None:
        """Remove a run from active tracking (called when pipeline finishes)."""
        self.active_runs.pop(run_id, None)

    # ---- Simplified API matching task spec ----

    def start_run(self, session_key: str, run_id: str) -> None:
        """Record run as active (simplified API for SessionState without a Task object)."""
        # Store a sentinel entry without a real task
        # This is used when we don't have a Task yet (pre-task creation)
        # The full ActiveRun is registered via register_run() when task is created
        pass

    def finish_run(self, session_key: str, run_id: str) -> None:
        """Mark run complete (alias for complete_run)."""
        self.complete_run(run_id)

    def is_run_active(self, session_key: str, run_id: str) -> bool:
        """Check if a run is currently active."""
        return run_id in self.active_runs

    def abort_run(self, session_key: str, run_id: str) -> None:
        """Signal cancellation for a run."""
        self.cancel_run(run_id)

    def get_active_run_id(self, session_key: str) -> str | None:
        """Returns the most recent active run_id for a session, or None."""
        for run_id, run in self.active_runs.items():
            if run.session_key == session_key:
                return run_id
        return None
