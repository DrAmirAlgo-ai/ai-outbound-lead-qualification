"""CSV upload → rule pre-filter → batched qualify → batched draft. No Apollo API.

Design goals after the free-tier quota incident:
- Spend as few LLM requests as possible (rules first, then batches of leads).
- Persist every result the moment it arrives, so a crash or quota stop never
  loses work. Leads that were not processed simply stay `new` / `qualified`.
- When Gemini quota is exhausted, stop cleanly with a clear message instead of
  grinding through 40 failures.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import csv_import
import db
import llm
import prefilter
import sending
from settings_store import load_settings

ProgressFn = Callable[[str, float, str], None]


def _progress(cb: ProgressFn | None, stage: str, frac: float, message: str) -> None:
    if cb:
        cb(stage, frac, message)


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


def qualify_new_leads(cb: ProgressFn | None = None) -> dict[str, Any]:
    settings = load_settings()
    threshold = int(settings["qualify_min_score"])
    batch_size = int(settings.get("qualify_batch_size") or 10)
    use_rules = bool(settings.get("rule_prefilter", True))

    leads = db.get_leads_by_status("new")
    total = len(leads)
    stats: dict[str, Any] = {
        "total": total,
        "rule_rejected": 0,
        "above": 0,
        "below": 0,
        "failed": 0,
        "requests": 0,
        "last_error": "",
        "paused_reason": "",
    }
    if not leads:
        return stats

    # 1) Rules: free, instant.
    to_llm: list[dict[str, Any]] = []
    for lead in leads:
        if use_rules:
            verdict, reason = prefilter.evaluate(lead)
            if verdict == "reject":
                db.update_qualification(lead["id"], 2, reason, None, "rejected")
                stats["rule_rejected"] += 1
                continue
        to_llm.append(lead)
    _progress(cb, "qualify", 0.1, f"الفلتر السريع رفض {stats['rule_rejected']} · {len(to_llm)} رايحين للعقل")

    # 2) LLM in batches.
    batches = _chunks(to_llm, batch_size)
    for bi, batch in enumerate(batches, start=1):
        names = ", ".join((b.get("company_name") or b.get("contact_name") or "?")[:18] for b in batch[:3])
        _progress(cb, "qualify", 0.1 + 0.5 * (bi - 1) / max(1, len(batches)), f"تقييم دفعة {bi}/{len(batches)}: {names}…")
        try:
            results = llm.qualify_leads_batch(batch)
            stats["requests"] += 1
        except llm.QuotaExhausted as exc:
            stats["paused_reason"] = str(exc)
            stats["last_error"] = llm.short_llm_error(exc)
            return stats
        except Exception as exc:  # noqa: BLE001 — show the real failure, don't hang silently
            stats["failed"] += len(batch)
            stats["last_error"] = llm.short_llm_error(exc)
            continue

        for lead in batch:
            res = results.get(int(lead["id"]))
            if not res:
                stats["failed"] += 1
                continue
            status = "qualified" if res["score"] >= threshold else "rejected"
            db.update_qualification(lead["id"], res["score"], res["reasoning"], res["hook"], status)
            if status == "qualified":
                stats["above"] += 1
            else:
                stats["below"] += 1

    return stats


def draft_qualified_leads(cb: ProgressFn | None = None) -> dict[str, Any]:
    settings = load_settings()
    batch_size = int(settings.get("draft_batch_size") or 3)
    leads = db.get_leads_by_status("qualified")
    peer_codes = db.company_code_map(leads)
    leads.sort(key=lambda item: (peer_codes.get(int(item["id"])) or "", int(item["id"])))
    stats: dict[str, Any] = {
        "total": len(leads),
        "ok": 0,
        "failed": 0,
        "requests": 0,
        "last_error": "",
        "paused_reason": "",
    }
    if not leads:
        return stats

    batches = _chunks(leads, batch_size)
    for bi, batch in enumerate(batches, start=1):
        names = ", ".join((b.get("company_name") or b.get("contact_name") or "?")[:18] for b in batch)
        _progress(cb, "draft", 0.6 + 0.38 * (bi - 1) / max(1, len(batches)), f"كتابة دفعة {bi}/{len(batches)}: {names}")
        has_peer = any(peer_codes.get(int(lead["id"])) for lead in batch)
        try:
            if batch_size <= 1 or has_peer:
                results = {}
                for lead in batch:
                    results[int(lead["id"])] = sending.draft_with_company_diversity(lead)
                    stats["requests"] += 1
                    sending.persist_draft(
                        lead["id"],
                        results[int(lead["id"])]["subject"],
                        results[int(lead["id"])]["body"],
                        lead.get("suggested_hook") or "",
                        kind="initial",
                    )
                    db.update_lead_status(lead["id"], "drafted")
                    stats["ok"] += 1
                continue
            try:
                results = llm.draft_emails_batch(batch)
                stats["requests"] += 1
            except llm.QuotaExhausted:
                raise
            except Exception:
                results = {}
        except llm.QuotaExhausted as exc:
            stats["paused_reason"] = str(exc)
            stats["last_error"] = llm.short_llm_error(exc)
            return stats
        except Exception as exc:  # noqa: BLE001
            stats["last_error"] = llm.short_llm_error(exc)
            results = {}

        missing = [lead for lead in batch if int(lead["id"]) not in results]
        if missing:
            _progress(cb, "draft", 0.7, f"بكتب {len(missing)} إيميل واحد واحد")
            for lead in missing:
                try:
                    results[int(lead["id"])] = sending.draft_with_company_diversity(lead)
                    stats["requests"] += 1
                except llm.QuotaExhausted as exc:
                    stats["paused_reason"] = str(exc)
                    stats["last_error"] = llm.short_llm_error(exc)
                    break
                except Exception as exc:  # noqa: BLE001
                    stats["failed"] += 1
                    stats["last_error"] = llm.short_llm_error(exc)
                    continue

            if stats.get("paused_reason"):
                return stats

        for lead in batch:
            draft = results.get(int(lead["id"]))
            if not draft:
                stats["failed"] += 1
                continue
            sending.persist_draft(lead["id"], draft["subject"], draft["body"], lead.get("suggested_hook") or "", kind="initial")
            db.update_lead_status(lead["id"], "drafted")
            stats["ok"] += 1

    return stats


def run_full_pipeline(cb: ProgressFn | None = None) -> dict[str, Any]:
    started = time.time()
    summary: dict[str, Any] = {"ingest": None, "qualify": None, "draft": None, "paused_reason": "", "message": ""}

    _progress(cb, "qualify", 0.05, "Scoring prospects...")
    q = qualify_new_leads(cb)
    summary["qualify"] = q
    if q.get("paused_reason"):
        summary["paused_reason"] = q["paused_reason"]
        summary["message"] = _summary_line(summary, started)
        return summary

    _progress(cb, "draft", 0.6, "Synthesizing drafts...")
    d = draft_qualified_leads(cb)
    summary["draft"] = d
    if d.get("paused_reason"):
        summary["paused_reason"] = d["paused_reason"]

    summary["message"] = _summary_line(summary, started)
    _progress(cb, "done", 1.0, summary["message"])
    return summary


def _summary_line(summary: dict[str, Any], started: float) -> str:
    q = summary.get("qualify") or {}
    d = summary.get("draft") or {}
    rejected = int(q.get("below", 0) or 0) + int(q.get("rule_rejected", 0) or 0)
    parts = [
        f"Qualified {int(q.get('above', 0) or 0)}",
        f"Disqualified {rejected}",
        f"Drafts {int(d.get('ok', 0) or 0)}",
    ]
    failed = int(q.get("failed", 0) or 0) + int(d.get("failed", 0) or 0)
    if failed:
        parts.append(f"Incomplete {failed}")
    return " — ".join(parts)


def run_from_upload(file_bytes: bytes, filename: str = "", cb: ProgressFn | None = None) -> dict[str, Any]:
    _progress(cb, "import", 0.02, "Ingesting file...")
    ingest = csv_import.import_csv(file_bytes, filename)
    summary = run_full_pipeline(cb)
    summary["ingest"] = ingest
    return summary
