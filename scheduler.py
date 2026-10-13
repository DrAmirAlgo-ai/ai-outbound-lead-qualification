"""Background worker. Follow-up drafts are prepared only when you click تجهيز المستحق."""

from __future__ import annotations

import threading
import time
import traceback

import db
from settings_store import load_env

FOLLOWUP_WORKER = "followups"
_STARTED = False
_LOCK = threading.Lock()


def run_followups_once(limit: int = 3, pace: bool = True) -> dict[str, int]:
    import sending

    try:
        stats = sending.process_due_followups(limit=limit, pace=pace)
    except Exception as exc:  # noqa: BLE001 — record and re-raise for the caller
        db.worker_heartbeat(
            FOLLOWUP_WORKER,
            ok=False,
            error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}"[:1500],
        )
        raise
    summary = (
        f"اتجهز {stats.get('drafted', 0)} للمراجعة · إعادة {stats.get('retry', 0)} · "
        f"اتخطى {stats.get('skipped', 0)} · فشل {stats.get('failed', 0)}"
    )
    db.worker_heartbeat(FOLLOWUP_WORKER, ok=True, summary=summary)
    return stats


def loop(poll_seconds: int = 45) -> None:
    load_env()
    db.init_db()
    db.worker_heartbeat(FOLLOWUP_WORKER, ok=True, summary="التجهيز يدوي")
    while True:
        time.sleep(poll_seconds)


def start_background() -> bool:
    global _STARTED
    with _LOCK:
        if _STARTED:
            return False
        thread = threading.Thread(target=loop, daemon=True, name="ai-outbound-worker")
        thread.start()
        _STARTED = True
        return True
