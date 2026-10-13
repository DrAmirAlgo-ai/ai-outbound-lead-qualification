"""Run the qualify→draft pipeline in a background thread.

The dashboard never blocks on the LLM: it starts a job, then polls the `jobs`
table for progress. One pipeline job at a time (per process via a lock; across
processes via the DB heartbeat).
"""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable

import db
import llm

JOB_KIND = "pipeline"
_LOCK = threading.Lock()
_THREADS: dict[str, threading.Thread] = {}


def _thread_alive(kind: str) -> bool:
    thread = _THREADS.get(kind)
    return thread is not None and thread.is_alive()


def is_running(kind: str = JOB_KIND) -> bool:
    with _LOCK:
        if _thread_alive(kind):
            return True
    return db.get_running_job(kind) is not None


def _start(kind: str, target: Callable[[int], None], name: str) -> int | None:
    with _LOCK:
        if _thread_alive(kind):
            return None
        if db.get_running_job(kind) is not None:
            return None
        job_id = db.create_job(kind, "جاري البدء...")
        thread = threading.Thread(target=target, args=(job_id,), daemon=True, name=name)
        _THREADS[kind] = thread
        thread.start()
        return job_id


def start_pipeline() -> int | None:
    """Start a background pipeline run. Returns job id, or None if one is already running."""
    return _start(JOB_KIND, _run_pipeline, "ai-outbound-pipeline")


def _run_pipeline(job_id: int) -> None:
    import pipeline

    def cb(stage: str, frac: float, message: str) -> None:
        db.update_job(job_id, progress=frac, message=message)

    try:
        summary: dict[str, Any] = pipeline.run_full_pipeline(cb=cb)
        status = "done"
        msg = summary.get("message") or "Complete"
        if summary.get("paused_reason"):
            status = "paused"
            msg = "Could not complete"
        else:
            q = summary.get("qualify") or {}
            d = summary.get("draft") or {}
            failed = int(q.get("failed") or 0) + int(d.get("failed") or 0)
            produced = int(q.get("above") or 0) + int(d.get("ok") or 0)
            if failed > 0 and produced == 0:
                status = "failed"
                msg = "Could not complete"
        db.update_job(job_id, progress=1.0, message=msg, status=status, summary=summary)
    except llm.QuotaExhausted as exc:
        db.update_job(job_id, status="paused", message="Could not complete", summary={"paused_reason": str(exc)})
    except Exception as exc:  # noqa: BLE001 — surface everything to the UI
        db.update_job(
            job_id,
            status="failed",
            message="Could not complete",
            summary={"traceback": traceback.format_exc()[-2000:]},
        )
