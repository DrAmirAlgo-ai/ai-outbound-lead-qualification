"""Send + follow-up scheduling. Human approval is still required for the first email."""

from __future__ import annotations

import importlib
import random
import sqlite3
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

import db
import emailer
import llm
from settings_store import load_settings

# Streamlit may keep an old emailer in memory after the attachments argument was added.
emailer = importlib.reload(emailer)

RETRY_COOLDOWN = timedelta(minutes=15)

_RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "temporar",
    "connection",
    "network",
    "429",
    "503",
    "500",
    "rate",
    "busy",
    "quota",
    "reset",
    "refused",
    "unavailable",
    "ssl",
    "broken pipe",
    "winerror",
    "10054",
    "10060",
    "10053",
    "10061",
    "try again",
    "high demand",
    "حصة",
    "مشغول",
    "شبكة",
)

_PERMANENT_MARKERS = (
    "authentication",
    "invalid recipient",
    "535",
    "username and password",
    "mفتاح",
    "مرفوض",
    "unexpected model name",
)


def render_greeting(contact_name: str | None, template: str | None = None) -> str:
    """Fill the greeting template from settings. Supports {first_name}, {full_name}, {name}."""
    if template is None:
        template = load_settings().get("greeting")
    tpl = (template or "").strip()
    if not tpl:
        return ""
    full = (contact_name or "").strip()
    first = full.split()[0] if full else ""
    # Fallback: no name → "Hi there," rather than "Hi ,".
    first = first or "there"
    full = full or "there"
    try:
        return tpl.format(first_name=first, full_name=full, name=first)
    except (KeyError, IndexError, ValueError):
        return tpl


def compose_with(
    greeting_tpl: str | None,
    signoff: str | None,
    signature: str | None,
    body: str,
    contact_name: str | None = None,
) -> str:
    """Final email text = greeting → body → sign-off → signature.

    The LLM body is scrubbed of any greeting / sign-off / signature first, so the
    only greeting and closing in the email are the ones passed in (from settings).
    Layout: greeting, blank line, body, blank line, sign-off, signature directly under it.
    """
    text = llm.clean_llm_body(body, contact_name)
    sig = (signature or "").strip()
    signoff = (signoff or "").strip()
    if signoff and sig.lower().startswith(signoff.lower()):
        signoff = ""
    greeting = render_greeting(contact_name, template=greeting_tpl or "")

    parts: list[str] = []
    if greeting:
        parts.append(greeting)
    parts.append(text)
    footer = "\n".join(p for p in (signoff, sig) if p)
    if footer:
        parts.append(footer)
    return "\n\n".join(p for p in parts if p)


def _pick_frame_part(explicit: str | None, draft_val: object, settings_val: object) -> str | None:
    if explicit is not None:
        return explicit
    if draft_val is not None:
        return str(draft_val)
    if settings_val is None:
        return None
    return str(settings_val)


def resolve_frame(
    greeting: str | None = None,
    signoff: str | None = None,
    signature: str | None = None,
    draft: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Card override → draft override → global settings. Empty string is a real override."""
    settings = load_settings()
    row = draft or {}
    return (
        _pick_frame_part(greeting, row.get("greeting"), settings.get("greeting")),
        _pick_frame_part(signoff, row.get("signoff"), settings.get("signoff")),
        _pick_frame_part(signature, row.get("signature"), settings.get("signature")),
    )


def drive_links_block(settings: dict[str, Any] | None = None) -> str:
    settings = settings or load_settings()
    folder = str(settings.get("drive_folder_url") or "").strip()
    if not folder:
        folder = str(settings.get("drive_case_study_url") or "").strip()
    if not folder:
        return ""
    return f"Folder: {folder}"


def append_drive_links(body: str, kind: str, settings: dict[str, Any] | None = None) -> str:
    if kind != "followup_2":
        return body
    block = drive_links_block(settings)
    if not block or block in (body or ""):
        return body
    return (body or "").rstrip() + "\n\n" + block


def compose_body(
    body: str,
    contact_name: str | None = None,
    greeting: str | None = None,
    signoff: str | None = None,
    signature: str | None = None,
    draft: dict[str, Any] | None = None,
    kind: str | None = None,
) -> str:
    """compose_with() using card/draft overrides, else the greeting / sign-off / signature in settings."""
    g, so, sig = resolve_frame(greeting, signoff, signature, draft)
    text = compose_with(g, so, sig, body, contact_name)
    if kind:
        text = append_drive_links(text, kind)
    return text


def preview_email(
    contact_name: str | None,
    subject: str,
    body: str,
    greeting: str | None = None,
    signoff: str | None = None,
    signature: str | None = None,
    draft: dict[str, Any] | None = None,
    kind: str | None = None,
) -> dict[str, str]:
    """What will actually be sent — used by the review tab."""
    return {
        "subject": subject,
        "body": compose_body(
            body,
            contact_name,
            greeting=greeting,
            signoff=signoff,
            signature=signature,
            draft=draft,
            kind=kind,
        ),
    }


def remaining_today() -> int:
    return 10_000


def persist_draft(
    lead_id: int,
    subject: str,
    body: str,
    hook: str,
    kind: str = "initial",
) -> int:
    """Save a draft and attach an Arabic review copy. English is what gets sent."""
    subject_ar = ""
    body_ar = ""
    try:
        ar = llm.arabic_review_copy(subject, body)
        subject_ar = ar.get("subject") or ""
        body_ar = ar.get("body") or ""
    except llm.LLMError:
        pass
    except Exception:
        pass
    version = db.next_draft_version(lead_id)
    return db.insert_draft(
        lead_id,
        subject,
        body,
        hook,
        version,
        kind=kind,
        subject_ar=subject_ar,
        body_ar=body_ar,
    )


def draft_with_company_diversity(
    lead: dict[str, Any],
    hook: str | None = None,
    kind: str = "initial",
    original_subject: str = "",
    original_body: str = "",
    step: int = 1,
) -> dict[str, str]:
    """Write one email, forced different if a colleague at the same firm already has a draft."""
    hook = hook if hook is not None else (lead.get("hook_used") or lead.get("suggested_hook") or "")
    siblings = db.sibling_email_context(int(lead["id"]), kind=kind) if hasattr(db, "sibling_email_context") else []
    variant = db.company_variant_index(int(lead["id"])) if hasattr(db, "company_variant_index") else 0
    extra = {}
    if "sibling_emails" in llm.draft_email.__code__.co_varnames:
        extra = {"sibling_emails": siblings, "variant_index": variant}
    if kind == "initial":
        return llm.draft_email(
            lead.get("contact_name") or "",
            lead.get("company_name") or "",
            hook,
            **extra,
        )
    fu_extra = extra if "sibling_emails" in llm.draft_followup.__code__.co_varnames else {}
    return llm.draft_followup(
        lead.get("contact_name") or "",
        lead.get("company_name") or "",
        hook,
        original_subject,
        original_body,
        step,
        **fu_extra,
    )


def _bodies_too_similar(body: str, others: list[str], threshold: float = 0.48) -> bool:
    left = " ".join((body or "").lower().split())
    if len(left) < 40:
        return False
    for other in others:
        right = " ".join((other or "").lower().split())
        if len(right) < 40:
            continue
        if SequenceMatcher(None, left, right).ratio() >= threshold:
            return True
    return False


def _send_with_attachments(
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    attachments: list | None = None,
    references: list[str] | None = None,
) -> str:
    """Call emailer.send_email, including attachments when this process has the new signature."""
    attach = attachments or []
    try:
        return emailer.send_email(
            to_addr,
            subject,
            body,
            in_reply_to=in_reply_to,
            attachments=attach,
            references=references,
        )
    except TypeError as exc:
        if "attachments" not in str(exc) and "references" not in str(exc):
            raise
        if attach and "attachments" in str(exc):
            raise emailer.SendError(
                "أعد تشغيل التطبيق من التيرمنال — نسخة الإرسال القديمة مش بتفهم المرفقات."
            ) from exc
        return emailer.send_email(to_addr, subject, body, in_reply_to=in_reply_to)


def diversify_same_company_drafts(rows: list[dict[str, Any]]) -> int:
    """If two drafted emails at one firm look too alike, rewrite the later one."""
    codes = db.company_code_map(rows)
    if not codes:
        return 0
    changed = 0
    ordered = sorted(rows, key=lambda r: int(r["id"]))
    for row in ordered:
        lid = int(row["id"])
        if lid not in codes:
            continue
        siblings = db.sibling_email_context(lid, kind="initial")
        if not siblings:
            continue
        if not _bodies_too_similar(row.get("body") or "", [s.get("body") or "" for s in siblings]):
            continue
        draft = draft_with_company_diversity(row, hook=row.get("hook_used") or row.get("suggested_hook") or "")
        persist_draft(
            lid,
            draft["subject"],
            draft["body"],
            row.get("hook_used") or row.get("suggested_hook") or "",
            kind="initial",
        )
        changed += 1
    return changed


def thread_subject(original_subject: str | None, fallback: str | None = None) -> str:
    """Keep follow-ups in the same thread by reusing Re: + the first subject."""
    orig = (original_subject or "").strip()
    if orig:
        bare = orig
        while bare.lower().startswith("re:"):
            bare = bare[3:].strip()
        return f"Re: {bare}" if bare else "Re:"
    return (fallback or "").strip()


def send_draft(
    lead: dict[str, Any],
    draft_id: int,
    subject: str,
    body: str,
    kind: str = "initial",
    greeting: str | None = None,
    signoff: str | None = None,
    signature: str | None = None,
) -> None:
    """Send one email. Idempotent per (lead, kind); daily cap is reserved atomically."""
    settings = load_settings()
    if settings.get("pause_sending"):
        raise emailer.SendError("الإرسال متوقف من الإعدادات.")
    lead_id = int(lead["id"])
    claimed = False

    if db.has_send(lead_id, kind):
        # Already went out (e.g. DB write raced with a rerun). Never send twice.
        if kind == "initial":
            db.update_lead_status(lead_id, "sent")
        raise emailer.SendError("الإيميل ده اتبعت قبل كده — مش هيتكرر.")

    db.reserve_daily_slot(0)

    if kind == "initial":
        if not db.claim_lead_for_initial_send(lead_id):
            db.release_daily_slot()
            if db.has_send(lead_id, "initial"):
                db.update_lead_status(lead_id, "sent")
            raise emailer.SendError("الإيميل ده بيتبعت دلوقتي أو اتبعت قبل كده — مش هيتكرر.")
        claimed = True

    in_reply_to = None
    references: list[str] | None = None
    if kind != "initial":
        original = db.initial_send_for_lead(lead_id)
        references = db.thread_message_ids(lead_id) if hasattr(db, "thread_message_ids") else []
        if not references:
            root = str((original or {}).get("message_id") or "").strip()
            references = [root] if root else []
        in_reply_to = references[-1] if references else None
        subject = thread_subject((original or {}).get("subject"), subject)

    stored = db.get_draft(draft_id) if hasattr(db, "get_draft") else None
    final_body = compose_body(
        body,
        lead.get("contact_name"),
        greeting=greeting,
        signoff=signoff,
        signature=signature,
        draft=stored,
        kind=kind,
    )
    # Follow-ups go out with the Drive folder link only. Never attach the old PDFs or video.
    attach = []
    if kind == "initial":
        try:
            import attachments as _att

            attach = _att.attachments_for_kind(kind)
        except Exception:
            attach = []
    try:
        message_id = _send_with_attachments(
            lead["contact_email"],
            subject,
            final_body,
            in_reply_to=in_reply_to,
            attachments=attach,
            references=references,
        )
    except Exception:
        db.release_daily_slot()
        if claimed:
            db.unclaim_lead_after_failed_send(lead_id)
        raise
    try:
        db.insert_send(draft_id, lead_id, kind=kind, message_id=message_id)
    except sqlite3.IntegrityError:
        db.update_lead_status(lead_id, "sent")
        raise emailer.SendError("الإيميل ده اتبعت قبل كده — مش هيتكرر.")
    db.update_lead_status(lead_id, "sent")
    if kind == "initial":
        _schedule_followups(lead_id)


def _schedule_followups(lead_id: int) -> None:
    """After the first email: schedule follow-up 1 only. Follow-up 2 waits for FU1 to send."""
    settings = load_settings()
    if not settings.get("followup_enabled"):
        return
    max_n = int(settings.get("max_followups") or 0)
    if max_n >= 1:
        db.schedule_followup(lead_id, 1, int(settings.get("followup_1_days") or 3))


def _schedule_next_followup(lead_id: int, sent_step: int) -> None:
    settings = load_settings()
    if not settings.get("followup_enabled"):
        return
    max_n = int(settings.get("max_followups") or 0)
    if sent_step == 1 and max_n >= 2:
        db.schedule_followup(lead_id, 2, int(settings.get("followup_2_days") or 4))
    if sent_step == 2 and max_n >= 3:
        db.schedule_followup_soon(lead_id, 3)


def _frame_update(
    greeting: str | None,
    signoff: str | None,
    signature: str | None,
) -> dict[str, str | None]:
    """Only persist frame fields the caller actually provided (empty string is provided)."""
    out: dict[str, str | None] = {}
    if greeting is not None:
        out["greeting"] = greeting
    if signoff is not None:
        out["signoff"] = signoff
    if signature is not None:
        out["signature"] = signature
    return out


def approve_and_send(
    lead: dict[str, Any],
    subject: str,
    body: str,
    hook: str,
    greeting: str | None = None,
    signoff: str | None = None,
    signature: str | None = None,
) -> str:
    """Save edits, then send. Sending only happens when you click إرسال."""
    db.update_draft(
        lead["draft_id"],
        subject,
        body,
        hook,
        **_frame_update(greeting, signoff, signature),
    )
    try:
        send_draft(
            lead,
            lead["draft_id"],
            subject,
            body,
            kind="initial",
            greeting=greeting,
            signoff=signoff,
            signature=signature,
        )
        return "sent"
    except emailer.SendError as exc:
        if not _is_duplicate_error(exc):
            db.update_lead_status(lead["id"], "drafted")
        raise


def approve_and_send_followup(
    row: dict[str, Any],
    subject: str,
    body: str,
    hook: str,
    greeting: str | None = None,
    signoff: str | None = None,
    signature: str | None = None,
) -> str:
    """Human approve of a follow-up draft — never sent until this is called."""
    fid = int(row["followup_id"])
    step = int(row["step"])
    kind = f"followup_{step}"
    db.update_draft(
        row["draft_id"],
        subject,
        body,
        hook,
        **_frame_update(greeting, signoff, signature),
    )
    if not db.claim_followup_for_send(fid):
        if db.has_send(int(row["id"]), kind):
            db.mark_followup(fid, "sent")
            _schedule_next_followup(int(row["id"]), step)
            return "sent"
        raise emailer.SendError("المتابعة دي بيتبعت دلوقتي أو اتبعت قبل كده — مش هتتكرر.")
    lead = {
        "id": row["id"],
        "contact_email": row["contact_email"],
        "contact_name": row.get("contact_name") or "",
    }
    try:
        send_draft(
            lead,
            row["draft_id"],
            subject,
            body,
            kind=kind,
            greeting=greeting,
            signoff=signoff,
            signature=signature,
        )
    except emailer.SendError as exc:
        if _is_duplicate_error(exc):
            db.mark_followup(fid, "sent", draft_id=row["draft_id"])
            _schedule_next_followup(int(row["id"]), step)
            raise
        db.unclaim_followup_after_failed_send(fid, str(exc)[:400])
        raise
    db.mark_followup(fid, "sent", draft_id=row["draft_id"])
    _schedule_next_followup(int(row["id"]), step)
    return "sent"


def _is_skip_error(exc: BaseException) -> bool:
    text = str(exc)
    return "الحد اليومي" in text or "متوقف من الإعدادات" in text


def _is_duplicate_error(exc: BaseException) -> bool:
    text = str(exc)
    return "اتبعت قبل كده" in text or "بيتبعت دلوقتي" in text


def _is_retryable(exc: BaseException) -> bool:
    if _is_skip_error(exc):
        return True
    if isinstance(exc, llm.QuotaExhausted):
        return True
    text = str(exc).lower()
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return False
    if isinstance(exc, (llm.LLMError, emailer.SendError)):
        return True
    return any(marker in text for marker in _RETRYABLE_MARKERS)


def _retry_at(exc: BaseException) -> datetime:
    retry_at = getattr(exc, "retry_at", None)
    if isinstance(retry_at, (int, float)) and retry_at > time.time():
        return datetime.fromtimestamp(retry_at)
    return datetime.now() + RETRY_COOLDOWN


def prepare_due_followups(limit: int = 5, pace: bool = False) -> dict[str, int]:
    """When a follow-up is due: write the draft and wait for human approve. Never sends."""
    settings = load_settings()
    stats = {"drafted": 0, "sent": 0, "skipped": 0, "failed": 0, "retry": 0}
    if not settings.get("followup_enabled"):
        return stats
    if db.is_weekend():
        stats["skipped"] = 1
        return stats

    if hasattr(db, "release_processing_followups"):
        db.release_processing_followups()
    claimed = db.claim_due_followups(limit=limit)
    for index, item in enumerate(claimed):
        fid = int(item["id"])
        step = int(item["step"])
        kind = f"followup_{step}"
        finished = False
        try:
            original = db.initial_send_for_lead(item["lead_id"])
            if not original:
                db.mark_followup(fid, "cancelled", error="no initial send")
                stats["skipped"] += 1
                finished = True
                continue

            if db.has_send(item["lead_id"], kind):
                db.mark_followup(fid, "sent", error=None)
                _schedule_next_followup(item["lead_id"], step)
                stats["skipped"] += 1
                finished = True
                continue

            existing = db.get_latest_draft(item["lead_id"], kind)
            if existing:
                db.mark_followup(fid, "ready", draft_id=int(existing["id"]))
                stats["drafted"] += 1
                finished = True
                continue

            draft = draft_with_company_diversity(
                {
                    "id": item["lead_id"],
                    "contact_name": item.get("contact_name") or "",
                    "company_name": item.get("company_name") or "",
                    "suggested_hook": item.get("suggested_hook") or original.get("hook_used") or "",
                },
                hook=item.get("suggested_hook") or original.get("hook_used") or "",
                kind=kind,
                original_subject=original.get("subject") or "",
                original_body=original.get("body") or "",
                step=step,
            )
            followup_subject = thread_subject(original.get("subject"), draft["subject"])
            draft_id = persist_draft(
                item["lead_id"],
                followup_subject,
                draft["body"],
                original.get("hook_used") or "",
                kind=kind,
            )
            db.mark_followup(fid, "ready", draft_id=draft_id)
            stats["drafted"] += 1
            finished = True
            more = index < len(claimed) - 1
            if pace and more:
                delay = random.randint(int(settings["delay_min_seconds"]), int(settings["delay_max_seconds"]))
                time.sleep(delay)
        except Exception as exc:  # noqa: BLE001 — keep going on the rest of the batch
            if _is_skip_error(exc):
                db.release_followup(fid, str(exc)[:400])
                stats["skipped"] += 1
                finished = True
                break
            db.mark_followup(fid, "failed", error=str(exc)[:400])
            stats["failed"] += 1
            finished = True
        finally:
            if not finished:
                db.release_followup(fid, "التجهيز اتقطع")
    return stats


def process_due_followups(limit: int = 5, pace: bool = False) -> dict[str, int]:
    """Back-compat name used by the worker: prepare drafts only, do not send."""
    return prepare_due_followups(limit=limit, pace=pace)


_FU_KEYS = (
    "followup_enabled",
    "followup_1_days",
    "followup_2_days",
    "followup_3_days",
    "max_followups",
)
_DRAFT_KEYS = ("greeting", "signoff", "signature", "sender_name", "sender_title")


def _setting_changed(previous: dict | None, current: dict, keys: tuple[str, ...]) -> bool:
    if not previous:
        return True
    for key in keys:
        left, right = previous.get(key), current.get(key)
        if isinstance(right, bool) or isinstance(left, bool):
            if bool(left) != bool(right):
                return True
        elif isinstance(right, int) or isinstance(left, int):
            try:
                if int(left or 0) != int(right or 0):
                    return True
            except (TypeError, ValueError):
                if left != right:
                    return True
        elif (left or "") != (right or ""):
            return True
    return False


def apply_settings_to_inflight(previous: dict | None = None) -> str:
    """Push current settings onto work that has not left the system (unsent)."""
    settings = load_settings()
    bits: list[str] = []
    extra_strip: list[str] = []
    if previous and _setting_changed(previous, settings, _DRAFT_KEYS):
        extra_strip = [
            str(previous.get("signature") or ""),
            str(previous.get("signoff") or ""),
            str(previous.get("sender_name") or ""),
            str(previous.get("sender_title") or ""),
            str(previous.get("greeting") or ""),
        ]
    if extra_strip or previous is None:
        n = db.refresh_unsent_drafts(llm.clean_llm_body, extra_strip=extra_strip or None)
        if n:
            bits.append(f"اتنظف {n} درافت لسه ما اتبعتش")
    if previous is None or _setting_changed(previous, settings, _FU_KEYS):
        fu = db.apply_inflight_followups(settings)
        if fu.get("fu_moved"):
            bits.append(f"اتظبط {fu['fu_moved']} متابعة")
        if fu.get("fu_cancelled"):
            bits.append(f"اتلغى {fu['fu_cancelled']}")
        if fu.get("fu_created"):
            bits.append(f"اتجدول {fu['fu_created']}")
    if previous is None or _setting_changed(previous, settings, ("qualify_min_score",)):
        q = db.apply_inflight_threshold(int(settings["qualify_min_score"]))
        if q.get("demoted"):
            bits.append(f"اترفض {q['demoted']} تحت الحد الجديد")
        if q.get("requalified"):
            bits.append(f"رجع {q['requalified']} للقبول")
    if previous and _setting_changed(previous, settings, ("offer", "draft_prompt", "followup_prompt", "qualify_prompt", "icp")):
        bits.append("التجهيز الجاي هيستخدم الصياغة الجديدة")
    if previous and _setting_changed(
        previous,
        settings,
        ("followup_2_prompt", "followup_3_prompt", "drive_folder_url", "drive_case_study_url"),
    ):
        n = db.reset_unsent_followup_drafts(2) if hasattr(db, "reset_unsent_followup_drafts") else 0
        if n:
            bits.append(f"هتتكتب تاني {n} متابعة 2")
        else:
            bits.append("برومبت المتابعة 2 للمتابعات اللي لسه هتتكتب")
    return " · ".join(bits)
