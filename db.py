"""SQLite persistence for leads, drafts, sends, follow-ups, and the daily send log."""

from __future__ import annotations

import os
import random
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")

_REJECT_EN_AR = {
    "Rule filter: title is a non-buyer role for trading-systems work.": "فلتر سريع: المنصب مش دور مشتري لأنظمة التداول",
    "Rule filter: no systematic-trading or asset-management signal in title, company, or keywords.": "فلتر سريع: مفيش إشارة تداول منظّم أو إدارة أصول في المنصب أو الشركة",
    "رفض يدوي من المراجعة": "رفض يدوي من المراجعة",
    "تحت حد القبول بعد تعديل الإعدادات": "تحت حد القبول بعد تعديل الإعدادات",
}


def _has_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text or ""))


def arabic_reject_reason(reason: str | None, source: str | None = None, score: object = None) -> str:
    """Keep the original details. Only map known English rule-filter lines to Arabic."""
    raw = (reason or "").strip()
    if not raw or raw == "—":
        return ""
    head = raw.split(" — ", 1)[0].strip()
    mapped = _REJECT_EN_AR.get(raw) or _REJECT_EN_AR.get(head)
    if mapped:
        extra = raw.split(" — ", 1)[1].strip() if " — " in raw else ""
        if extra and extra != mapped and extra not in mapped:
            return f"{mapped} — {extra}"
        return mapped
    if raw.lower().startswith("rule filter"):
        if "non-buyer" in raw.lower() or "title is" in raw.lower():
            return "فلتر سريع: المنصب مش دور مشتري لأنظمة التداول"
        return "فلتر سريع: مفيش إشارة تداول منظّم أو إدارة أصول في المنصب أو الشركة"
    return raw


ROOT = Path(__file__).resolve().parent
LEGACY_DB_PATH = ROOT / "aioutbound.db"


def _resolve_db_path() -> Path:
    """Keep SQLite (and its WAL side files) out of OneDrive-synced folders.

    Order: AIOUTBOUND_DB_PATH env → %LOCALAPPDATA%/AIOutbound/aioutbound.db when the
    project lives under OneDrive → project folder otherwise.
    """
    override = os.environ.get("AIOUTBOUND_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    if "onedrive" in str(ROOT).lower():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "AIOutbound" / "aioutbound.db"
    return LEGACY_DB_PATH


DB_PATH = _resolve_db_path()


def _migrate_legacy_db_file() -> None:
    """First run after relocation: copy the OneDrive DB (with WAL checkpoint) to the new home."""
    if DB_PATH == LEGACY_DB_PATH or DB_PATH.exists() or not LEGACY_DB_PATH.exists():
        return
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        src = sqlite3.connect(LEGACY_DB_PATH)
        try:
            dst = sqlite3.connect(DB_PATH)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error:
        shutil.copy2(LEGACY_DB_PATH, DB_PATH)


SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    company_name TEXT,
    contact_name TEXT,
    contact_email TEXT,
    title TEXT,
    source_notes TEXT,
    status TEXT DEFAULT 'new',
    fit_score INTEGER,
    fit_reasoning TEXT,
    suggested_hook TEXT,
    apollo_id TEXT,
    linkedin_url TEXT,
    organization_domain TEXT,
    last_contacted_at TIMESTAMP,
    reject_reason TEXT,
    reject_source TEXT,
    rejected_at TIMESTAMP,
    sending_claimed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    subject TEXT,
    body TEXT,
    hook_used TEXT,
    version INTEGER DEFAULT 1,
    kind TEXT DEFAULT 'initial',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY,
    draft_id INTEGER REFERENCES drafts(id),
    lead_id INTEGER REFERENCES leads(id),
    sent_at TIMESTAMP,
    replied BOOLEAN DEFAULT 0,
    replied_at TIMESTAMP,
    outcome TEXT,
    kind TEXT DEFAULT 'initial'
);

CREATE TABLE IF NOT EXISTS follow_ups (
    id INTEGER PRIMARY KEY,
    lead_id INTEGER REFERENCES leads(id),
    step INTEGER,
    scheduled_at TIMESTAMP,
    draft_id INTEGER,
    sent_at TIMESTAMP,
    status TEXT DEFAULT 'scheduled',
    last_error TEXT,
    claimed_at TIMESTAMP,
    claim_token TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_send_log (
    date TEXT PRIMARY KEY,
    count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT,
    status TEXT DEFAULT 'running',
    progress REAL DEFAULT 0,
    message TEXT,
    summary_json TEXT,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    heartbeat_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inbox_seen (
    folder TEXT,
    uid TEXT,
    seen_at TIMESTAMP,
    PRIMARY KEY (folder, uid)
);

CREATE TABLE IF NOT EXISTS worker_status (
    name TEXT PRIMARY KEY,
    last_run_at TIMESTAMP,
    last_ok_at TIMESTAMP,
    last_error TEXT,
    last_summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(contact_email);
CREATE INDEX IF NOT EXISTS idx_drafts_lead ON drafts(lead_id);
CREATE INDEX IF NOT EXISTS idx_sends_lead ON sends(lead_id);
CREATE INDEX IF NOT EXISTS idx_followups_status ON follow_ups(status);

CREATE TABLE IF NOT EXISTS company_codes (
    cluster_key TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def is_weekend(dt: datetime | None = None) -> bool:
    return (dt or datetime.now()).weekday() >= 5


def shift_off_weekend(dt: datetime) -> datetime:
    """If the datetime lands on Saturday, move to Monday; Sunday → Monday."""
    if dt.weekday() == 5:
        return dt + timedelta(days=2)
    if dt.weekday() == 6:
        return dt + timedelta(days=1)
    return dt


def add_business_days(start: datetime, days: int) -> datetime:
    """Advance `days` weekdays (Mon–Fri), then guarantee the result is not a weekend."""
    current = start
    remaining = max(0, int(days))
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return shift_off_weekend(current)


def humanize_send_time(
    when: datetime,
    jitter_min: int = 45,
    jitter_max: int = 240,
    window_start: int = 9,
    window_end: int = 17,
) -> datetime:
    """Push a follow-up off the exact clock-time of the first email.

    Adds ±[jitter_min, jitter_max] minutes (never less than jitter_min), then clamps
    into the local send window and keeps the result on a weekday.
    """
    jitter_min = max(0, int(jitter_min))
    jitter_max = max(jitter_min, int(jitter_max))
    offset = random.randint(jitter_min, jitter_max) if jitter_max > 0 else 0
    if random.random() < 0.5:
        offset = -offset
    candidate = when + timedelta(minutes=offset)

    ws = max(0, min(23, int(window_start)))
    we = max(ws + 1, min(24, int(window_end)))
    if candidate.hour < ws:
        candidate = candidate.replace(hour=ws, minute=random.randint(5, 55), second=random.randint(0, 59))
    elif candidate.hour >= we:
        candidate = candidate.replace(hour=we - 1, minute=random.randint(5, 55), second=random.randint(0, 59))
        if candidate <= when and candidate.date() == when.date():
            candidate = (candidate + timedelta(days=1)).replace(hour=ws, minute=random.randint(5, 55))
    return shift_off_weekend(candidate)


def parse_when(value: object) -> datetime | None:
    text = str(value or "").strip().replace("T", " ")
    if not text:
        return None
    for fmt, width in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(text[:width], fmt)
        except ValueError:
            continue
    return None


def _followup_moment(days: int, start: datetime | None = None) -> datetime:
    """Business days from `start` (or now). Weekends move to the next weekday. No jitter."""
    origin = start or datetime.now()
    return add_business_days(origin, max(1, int(days)))


def _soon_moment() -> datetime:
    """Same clock on the next weekday. Video follow-up waits for a manual prepare click."""
    return shift_off_weekend(datetime.now())


_SETTINGS_CANCEL_MARK = "اتلغت: إعدادات المتابعة"
_UNSET = object()


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, now_iso()),
    )


def _cancel_unsequenced_followup_2(conn: sqlite3.Connection) -> None:
    """One-time: drop step-2 rows that were scheduled together with a still-pending step 1."""
    conn.execute(
        """
        UPDATE follow_ups
        SET status = 'cancelled',
            last_error = 'اتلغت تلقائياً: المتابعة 2 بتتجدول بعد نجاح المتابعة 1',
            claim_token = NULL,
            claimed_at = NULL
        WHERE step = 2
          AND status IN ('scheduled', 'processing')
          AND lead_id IN (
              SELECT lead_id FROM (
                  SELECT lead_id FROM follow_ups
                  WHERE step = 1 AND status IN ('scheduled', 'processing')
              )
          )
        """
    )


def _rehome_weekend_followups(conn: sqlite3.Connection) -> int:
    """One-time: move every scheduled follow-up that sits on a weekend to the next weekday."""
    rows = conn.execute(
        "SELECT id, scheduled_at FROM follow_ups WHERE status = 'scheduled'"
    ).fetchall()
    moved = 0
    for row in rows:
        try:
            when = datetime.strptime(str(row["scheduled_at"]), "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if when.weekday() < 5:
            continue
        new_when = shift_off_weekend(when)
        conn.execute(
            "UPDATE follow_ups SET scheduled_at = ?, last_error = ? WHERE id = ?",
            (new_when.strftime("%Y-%m-%d %H:%M:%S"), "اتنقلت من الويكند ليوم عمل", int(row["id"])),
        )
        moved += 1
    return moved


def _resnap_scheduled_followups(conn: sqlite3.Connection) -> int:
    """Drop jitter / window overflow: put scheduled FU1/FU2 on N business days from the send."""
    try:
        from settings_store import load_settings

        settings = load_settings()
        days = {
            1: int(settings.get("followup_1_days") or 3),
            2: int(settings.get("followup_2_days") or 4),
        }
    except Exception:
        days = {1: 3, 2: 4}
    rows = conn.execute(
        """
        SELECT id, lead_id, step FROM follow_ups
        WHERE status IN ('scheduled', 'processing') AND step IN (1, 2)
        """
    ).fetchall()
    moved = 0
    for row in rows:
        step = int(row["step"])
        start = _anchor_for_followup(int(row["lead_id"]), step, conn=conn)
        when = _followup_moment(days.get(step, 3), start=start)
        conn.execute(
            """
            UPDATE follow_ups
            SET scheduled_at = ?, last_error = 'اتظبطت: من غير زحزحة'
            WHERE id = ?
            """,
            (when.strftime("%Y-%m-%d %H:%M:%S"), int(row["id"])),
        )
        moved += 1
    return moved


def _run_migrations(conn: sqlite3.Connection) -> None:
    if _meta_get(conn, "mig_sequential_followups") is None:
        _cancel_unsequenced_followup_2(conn)
        _meta_set(conn, "mig_sequential_followups", "done")
    if _meta_get(conn, "mig_weekend_rehome") is None:
        moved = _rehome_weekend_followups(conn)
        _meta_set(conn, "mig_weekend_rehome", str(moved))
    if _meta_get(conn, "mig_jitter_existing") is None:
        # Rows scheduled before jitter existed sit at the exact minute of the first
        # email. Spread them so the follow-up never mirrors the original send time.
        rows = conn.execute(
            """
            SELECT f.id, f.scheduled_at, s.sent_at
            FROM follow_ups f
            LEFT JOIN sends s ON s.lead_id = f.lead_id AND IFNULL(s.kind, 'initial') = 'initial'
            WHERE f.status = 'scheduled'
            """
        ).fetchall()
        moved = 0
        for row in rows:
            try:
                when = datetime.strptime(str(row["scheduled_at"]), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                continue
            sent = None
            try:
                sent = datetime.strptime(str(row["sent_at"]), "%Y-%m-%d %H:%M:%S") if row["sent_at"] else None
            except (TypeError, ValueError):
                sent = None
            if sent is not None and abs((when.hour * 60 + when.minute) - (sent.hour * 60 + sent.minute)) > 30:
                continue  # already clearly off the original minute
            new_when = humanize_send_time(when)
            conn.execute(
                "UPDATE follow_ups SET scheduled_at = ? WHERE id = ?",
                (new_when.strftime("%Y-%m-%d %H:%M:%S"), int(row["id"])),
            )
            moved += 1
        conn.execute(
            "UPDATE follow_ups SET last_error = ? WHERE last_error = ?",
            ("اتلغت تلقائياً: المتابعة 2 بتتجدول بعد نجاح المتابعة 1", "cancelled: follow-up 2 waits until follow-up 1 is sent"),
        )
        _meta_set(conn, "mig_jitter_existing", str(moved))
    if _meta_get(conn, "mig_drop_followup_jitter") is None:
        moved = _resnap_scheduled_followups(conn)
        _meta_set(conn, "mig_drop_followup_jitter", str(moved))
    if _meta_get(conn, "mig_unique_email") is None:
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email_unique ON leads(lower(contact_email))"
            )
            _meta_set(conn, "mig_unique_email", "done")
        except sqlite3.IntegrityError:
            _meta_set(conn, "mig_unique_email", "skipped: duplicates exist")
    if _meta_get(conn, "mig_reject_reason_backfill") is None:
        conn.execute(
            """
            UPDATE leads
            SET reject_reason = COALESCE(NULLIF(reject_reason, ''), fit_reasoning),
                reject_source = CASE
                    WHEN reject_source IS NOT NULL AND reject_source != '' THEN reject_source
                    WHEN IFNULL(fit_reasoning, '') LIKE 'Rule filter:%' THEN 'rule'
                    WHEN fit_score IS NOT NULL THEN 'llm'
                    ELSE 'unknown'
                END,
                rejected_at = COALESCE(rejected_at, created_at)
            WHERE status = 'rejected'
              AND (reject_reason IS NULL OR reject_reason = '')
            """
        )
        _meta_set(conn, "mig_reject_reason_backfill", "done")
    if _meta_get(conn, "mig_reject_reason_ar") is None:
        rows = conn.execute(
            """
            SELECT id, reject_reason, fit_reasoning, reject_source, fit_score
            FROM leads
            WHERE status = 'rejected'
            """
        ).fetchall()
        for row in rows:
            ar = arabic_reject_reason(
                row["reject_reason"] or row["fit_reasoning"],
                row["reject_source"],
                row["fit_score"],
            )
            conn.execute("UPDATE leads SET reject_reason = ? WHERE id = ?", (ar, row["id"]))
        _meta_set(conn, "mig_reject_reason_ar", str(len(rows)))
    if _meta_get(conn, "mig_restore_reject_details") is None:
        conn.execute(
            """
            UPDATE leads
            SET reject_reason = fit_reasoning
            WHERE status = 'rejected'
              AND IFNULL(fit_reasoning, '') != ''
              AND (
                IFNULL(reject_reason, '') LIKE '%مش مناسب للعميل المستهدف%'
                OR IFNULL(reject_reason, '') LIKE 'تقييم العقل:%'
              )
            """
        )
        _meta_set(conn, "mig_restore_reject_details", "done")
    if _meta_get(conn, "mig_clear_generic_reject_ar") is None:
        conn.execute(
            """
            UPDATE leads
            SET reject_reason_ar = NULL
            WHERE status = 'rejected'
              AND (
                IFNULL(reject_reason_ar, '') LIKE '%مش مناسب للعميل المستهدف%'
                OR IFNULL(reject_reason_ar, '') LIKE 'تقييم العقل:%'
                OR IFNULL(reject_reason_ar, '') LIKE '%لم يجتاز التقييم%'
              )
            """
        )
        _meta_set(conn, "mig_clear_generic_reject_ar", "done")
    if _meta_get(conn, "mig_unique_send_kind") is None:
        conn.execute(
            """
            DELETE FROM sends
            WHERE id NOT IN (
                SELECT MIN(id) FROM sends GROUP BY lead_id, COALESCE(kind, 'initial')
            )
            """
        )
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sends_lead_kind "
                "ON sends(lead_id, COALESCE(kind, 'initial'))"
            )
            _meta_set(conn, "mig_unique_send_kind", "done")
        except sqlite3.IntegrityError:
            _meta_set(conn, "mig_unique_send_kind", "skipped: duplicates remain")
    _release_stale_sending(conn)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _add_column(conn: sqlite3.Connection, table: str, name: str, spec: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_db_file()
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _add_column(conn, "leads", "suggested_hook", "TEXT")
        _add_column(conn, "leads", "apollo_id", "TEXT")
        _add_column(conn, "leads", "linkedin_url", "TEXT")
        _add_column(conn, "leads", "organization_domain", "TEXT")
        _add_column(conn, "leads", "last_contacted_at", "TIMESTAMP")
        _add_column(conn, "leads", "reject_reason", "TEXT")
        _add_column(conn, "leads", "reject_source", "TEXT")
        _add_column(conn, "leads", "rejected_at", "TIMESTAMP")
        _add_column(conn, "leads", "sending_claimed_at", "TIMESTAMP")
        _add_column(conn, "drafts", "kind", "TEXT DEFAULT 'initial'")
        _add_column(conn, "sends", "kind", "TEXT DEFAULT 'initial'")
        _add_column(conn, "sends", "message_id", "TEXT")
        _add_column(conn, "follow_ups", "claimed_at", "TIMESTAMP")
        _add_column(conn, "follow_ups", "claim_token", "TEXT")
        _add_column(conn, "drafts", "subject_ar", "TEXT")
        _add_column(conn, "drafts", "body_ar", "TEXT")
        _add_column(conn, "drafts", "greeting", "TEXT")
        _add_column(conn, "drafts", "signoff", "TEXT")
        _add_column(conn, "drafts", "signature", "TEXT")
        _add_column(conn, "leads", "linkedin_reached", "INTEGER DEFAULT 0")
        _add_column(conn, "leads", "reject_reason_ar", "TEXT")
        _add_column(conn, "leads", "outcome_note", "TEXT")
        _add_column(conn, "leads", "outcome_note_at", "TIMESTAMP")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_apollo ON leads(apollo_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sends_message_id ON sends(message_id)")
        _run_migrations(conn)
        conn.execute(
            """
            UPDATE leads
            SET status = 'drafted', sending_claimed_at = NULL
            WHERE status IN ('approved', 'sending')
              AND NOT EXISTS (
                  SELECT 1 FROM sends s
                  WHERE s.lead_id = leads.id AND IFNULL(s.kind, 'initial') = 'initial'
              )
            """
        )
    backfill_linkedin_urls()


def db_location() -> dict[str, Any]:
    return {
        "path": str(DB_PATH),
        "in_onedrive": "onedrive" in str(DB_PATH).lower(),
        "legacy_exists": LEGACY_DB_PATH.exists() and LEGACY_DB_PATH != DB_PATH,
    }


# ----------------------------------------------------------------------------- worker status


def worker_heartbeat(name: str, ok: bool, summary: str = "", error: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO worker_status (name, last_run_at, last_ok_at, last_error, last_summary)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                last_run_at = excluded.last_run_at,
                last_ok_at = CASE WHEN ? THEN excluded.last_run_at ELSE worker_status.last_ok_at END,
                last_error = excluded.last_error,
                last_summary = excluded.last_summary
            """,
            (name, now_iso(), now_iso() if ok else None, error or None, summary or None, 1 if ok else 0),
        )


def get_worker_status(name: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        return row_to_dict(conn.execute("SELECT * FROM worker_status WHERE name = ?", (name,)).fetchone())


def row_to_dict(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


def email_exists(email: str) -> bool:
    if not email:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT 1 FROM leads WHERE lower(contact_email) = lower(?) LIMIT 1",
            (email.strip(),),
        )
        return cur.fetchone() is not None


def apollo_id_exists(apollo_id: str) -> bool:
    if not apollo_id:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT 1 FROM leads WHERE apollo_id = ? LIMIT 1",
            (apollo_id.strip(),),
        )
        return cur.fetchone() is not None


def insert_lead(
    company_name: str,
    contact_name: str,
    contact_email: str,
    title: str,
    source_notes: str,
    apollo_id: str = "",
    linkedin_url: str = "",
    organization_domain: str = "",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO leads (
                company_name, contact_name, contact_email, title, source_notes, status,
                apollo_id, linkedin_url, organization_domain
            )
            VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?)
            """,
            (
                (company_name or "").strip(),
                (contact_name or "").strip(),
                (contact_email or "").strip(),
                (title or "").strip(),
                (source_notes or "").strip(),
                (apollo_id or "").strip() or None,
                (linkedin_url or "").strip() or None,
                (organization_domain or "").strip() or None,
            ),
        )
        return int(cur.lastrowid)


def normalize_linkedin_url(url: str | None) -> str:
    text = (url or "").strip().rstrip(").,;]}>\"'")
    if not text:
        return ""
    lower = text.lower()
    if lower.startswith("linkedin.com"):
        text = "https://www." + text
    elif lower.startswith("www.linkedin.com"):
        text = "https://" + text
    elif lower.startswith("http://"):
        text = "https://" + text[len("http://") :]
    text = text.split()[0].strip()
    if "linkedin.com/" not in text.lower():
        return ""
    return text.rstrip("/")


_LINKEDIN_URL_RE = re.compile(
    r"https?://(?:[\w.-]+\.)?linkedin\.com/(?:in|pub|company|school)/[^\s,;|<>\"']+",
    re.I,
)


def extract_linkedin_url(*texts: str | None) -> str:
    """Prefer a personal /in/ profile; otherwise a company page found in notes or columns."""
    found_person: list[str] = []
    found_other: list[str] = []
    for raw in texts:
        if not raw:
            continue
        direct = normalize_linkedin_url(raw)
        if direct:
            (found_person if "/in/" in direct.lower() or "/pub/" in direct.lower() else found_other).append(direct)
        for match in _LINKEDIN_URL_RE.finditer(str(raw)):
            url = normalize_linkedin_url(match.group(0))
            if not url:
                continue
            if "/in/" in url.lower() or "/pub/" in url.lower():
                found_person.append(url)
            else:
                found_other.append(url)
    return (found_person or found_other or [""])[0]


def linkedin_search_url(contact_name: str | None, company_name: str | None) -> str:
    from urllib.parse import quote_plus

    query = " ".join(p for p in ((contact_name or "").strip(), (company_name or "").strip()) if p)
    if not query:
        return ""
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(query)


def backfill_linkedin_urls() -> int:
    """Pull LinkedIn URLs out of source_notes into linkedin_url for older rows."""
    changed = 0
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, linkedin_url, source_notes
            FROM leads
            WHERE linkedin_url IS NULL OR trim(linkedin_url) = ''
            """
        ).fetchall()
        for row in rows:
            url = extract_linkedin_url(row["linkedin_url"], row["source_notes"])
            if not url:
                continue
            conn.execute("UPDATE leads SET linkedin_url = ? WHERE id = ?", (url, int(row["id"])))
            changed += 1
    return changed


def update_linkedin_if_empty(email: str, url: str) -> bool:
    url = extract_linkedin_url(url)
    if not url or not (email or "").strip():
        return False
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE leads
            SET linkedin_url = ?
            WHERE lower(contact_email) = lower(?)
              AND (linkedin_url IS NULL OR trim(linkedin_url) = '')
            """,
            (url, email.strip()),
        )
        return cur.rowcount == 1


def get_linkedin_directory() -> list[dict[str, Any]]:
    """Qualified / approved / already-contacted leads with the best LinkedIn link we have."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                company_name,
                contact_name,
                contact_email,
                title,
                status,
                fit_score,
                linkedin_url,
                linkedin_reached,
                source_notes
            FROM leads
            WHERE status IN ('qualified', 'drafted', 'approved', 'sending', 'sent', 'replied', 'closed')
            ORDER BY COALESCE(fit_score, 0) DESC, id DESC
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        url = extract_linkedin_url(item.get("linkedin_url"), item.get("source_notes"))
        if url and ("/in/" in url.lower() or "/pub/" in url.lower()):
            kind = "person"
        elif url:
            kind = "company"
        else:
            kind = "search"
            url = linkedin_search_url(item.get("contact_name"), item.get("company_name"))
        item["linkedin_url"] = url
        item["linkedin_kind"] = kind
        item["linkedin_reached"] = bool(item.get("linkedin_reached"))
        item.pop("source_notes", None)
        out.append(item)
    return out


def set_linkedin_reached(lead_id: int, reached: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET linkedin_reached = ? WHERE id = ?",
            (1 if reached else 0, lead_id),
        )


def get_leads_by_status(status: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE status = ? ORDER BY id DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_leads_by_statuses(statuses: list[str]) -> list[dict[str, Any]]:
    if not statuses:
        return []
    marks = ",".join("?" for _ in statuses)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM leads WHERE status IN ({marks}) ORDER BY id ASC",
            tuple(statuses),
        ).fetchall()
        return [dict(r) for r in rows]


# ----------------------------------------------------------------------------- jobs


def create_job(kind: str, message: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, status, progress, message, heartbeat_at) VALUES (?, 'running', 0, ?, ?)",
            (kind, message, now_iso()),
        )
        return int(cur.lastrowid)


def update_job(
    job_id: int,
    *,
    progress: float | None = None,
    message: str | None = None,
    status: str | None = None,
    summary: dict[str, Any] | None = None,
) -> None:
    sets = ["heartbeat_at = ?"]
    vals: list[Any] = [now_iso()]
    if progress is not None:
        sets.append("progress = ?")
        vals.append(float(progress))
    if message is not None:
        sets.append("message = ?")
        vals.append(message)
    if status is not None:
        sets.append("status = ?")
        vals.append(status)
        if status != "running":
            sets.append("finished_at = ?")
            vals.append(now_iso())
    if summary is not None:
        import json as _json

        sets.append("summary_json = ?")
        vals.append(_json.dumps(summary, ensure_ascii=False))
    vals.append(job_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", tuple(vals))


def get_latest_job(kind: str | None = None) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        if kind:
            row = conn.execute(
                "SELECT * FROM jobs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
        data = row_to_dict(row)
        if data and data.get("summary_json"):
            import json as _json

            try:
                data["summary"] = _json.loads(data["summary_json"])
            except _json.JSONDecodeError:
                data["summary"] = None
        return data


def get_running_job(kind: str | None = None, stale_minutes: int = 20) -> Optional[dict[str, Any]]:
    """A job is 'running' only if its heartbeat is fresh; otherwise it is treated as dead."""
    job = get_latest_job(kind)
    if not job or job.get("status") != "running":
        return None
    hb = job.get("heartbeat_at") or job.get("started_at")
    try:
        last = datetime.strptime(str(hb), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return job
    if datetime.now() - last > timedelta(minutes=stale_minutes):
        update_job(int(job["id"]), status="failed", message="الجوب اتقطع (مفيش نبض).")
        return None
    return job


def count_by_status() -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM leads GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}


def update_qualification(
    lead_id: int,
    score: int,
    reasoning: str,
    hook: Optional[str],
    status: str,
) -> None:
    reject_reason = None
    reject_source = None
    rejected_at = None
    if status == "rejected":
        rejected_at = now_iso()
        if (reasoning or "").startswith("Rule filter") or (reasoning or "").startswith("فلتر سريع"):
            reject_source = "rule"
        else:
            reject_source = "llm"
        reject_reason = arabic_reject_reason(reasoning, reject_source, score)
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE leads
            SET fit_score = ?,
                fit_reasoning = ?,
                suggested_hook = ?,
                status = ?,
                reject_reason = COALESCE(?, reject_reason),
                reject_source = COALESCE(?, reject_source),
                rejected_at = COALESCE(?, rejected_at)
            WHERE id = ?
            """,
            (score, reasoning, hook, status, reject_reason, reject_source, rejected_at, lead_id),
        )


def update_lead_status(lead_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))


def reject_lead(lead_id: int, reason: str, source: str = "manual") -> None:
    reason = arabic_reject_reason((reason or "").strip() or "رفض يدوي من المراجعة", source)
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE leads
            SET status = 'rejected',
                reject_reason = ?,
                reject_source = ?,
                rejected_at = ?
            WHERE id = ?
            """,
            (reason, source, now_iso(), lead_id),
        )


def get_rejected_leads() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                id,
                company_name,
                contact_name,
                contact_email,
                title,
                fit_score,
                linkedin_url,
                source_notes,
                COALESCE(NULLIF(reject_reason, ''), fit_reasoning, '—') AS reject_reason,
                fit_reasoning,
                reject_reason_ar,
                COALESCE(reject_source, 'unknown') AS reject_source,
                rejected_at,
                created_at
            FROM leads
            WHERE status = 'rejected'
            ORDER BY COALESCE(rejected_at, created_at) DESC, id DESC
            """
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            original = (item.get("fit_reasoning") or item.get("reject_reason") or "").strip()
            mapped = arabic_reject_reason(original, item.get("reject_source"), item.get("fit_score"))
            generic = "مش مناسب للعميل المستهدف" in (item.get("reject_reason") or "") or str(
                item.get("reject_reason") or ""
            ).startswith("تقييم العقل:")
            if generic and original:
                item["reject_reason"] = original
            elif mapped:
                item["reject_reason"] = mapped
            item["reject_detail"] = (item.get("reject_reason_ar") or "").strip() or item.get("reject_reason") or original
            item["linkedin_url"] = extract_linkedin_url(
                item.get("linkedin_url"), item.get("source_notes")
            ) or linkedin_search_url(item.get("contact_name"), item.get("company_name"))
            item.pop("source_notes", None)
            out.append(item)
        return out


def restore_rejected_to_draft(lead_id: int) -> str:
    """Move a rejected lead back to the draft review queue (or qualify if no draft yet)."""
    draft = get_latest_draft(lead_id, "initial")
    status = "drafted" if draft else "qualified"
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE leads
            SET status = ?, sending_claimed_at = NULL
            WHERE id = ? AND status = 'rejected'
            """,
            (status, lead_id),
        )
    return status


def set_reject_reason_ar(lead_id: int, text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET reject_reason_ar = ? WHERE id = ?",
            ((text or "").strip(), lead_id),
        )


def claim_lead_for_initial_send(lead_id: int) -> bool:
    """Atomically take exclusive rights to send the first email. Prevents double-click sends."""
    stamp = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE leads
            SET status = 'sending', sending_claimed_at = ?
            WHERE id = ?
              AND status IN ('drafted', 'approved')
              AND NOT EXISTS (
                  SELECT 1 FROM sends
                  WHERE lead_id = leads.id AND COALESCE(kind, 'initial') = 'initial'
              )
            """,
            (stamp, lead_id),
        )
        return cur.rowcount == 1


def unclaim_lead_after_failed_send(lead_id: int) -> None:
    """SMTP failed after the claim — put the lead back in review."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE leads
            SET status = 'drafted', sending_claimed_at = NULL
            WHERE id = ?
              AND status = 'sending'
              AND NOT EXISTS (
                  SELECT 1 FROM sends
                  WHERE lead_id = leads.id AND COALESCE(kind, 'initial') = 'initial'
              )
            """,
            (lead_id,),
        )


def _release_stale_sending(conn: sqlite3.Connection, minutes: int = 10) -> None:
    """Crash recovery: a 'sending' claim with no send row older than a few minutes."""
    cutoff = (datetime.now() - timedelta(minutes=max(1, minutes))).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        UPDATE leads
        SET status = 'drafted', sending_claimed_at = NULL
        WHERE status = 'sending'
          AND (sending_claimed_at IS NULL OR sending_claimed_at < ?)
          AND NOT EXISTS (
              SELECT 1 FROM sends s
              WHERE s.lead_id = leads.id AND COALESCE(s.kind, 'initial') = 'initial'
          )
        """,
        (cutoff,),
    )


_GENERIC_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "mail.com",
    "gmx.com",
    "gmx.de",
    "yandex.com",
    "yandex.ru",
}

_COMPANY_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|gmbh|ag|sa|sas|plc|co|corp|corporation|limited|group|holdings?|bv|nv|oy|ab|pty)\b",
    re.I,
)


def email_domain(email: str | None, organization_domain: str | None = None) -> str:
    org = (organization_domain or "").strip().lower().lstrip("www.")
    if org and org not in _GENERIC_EMAIL_DOMAINS:
        return org
    addr = (email or "").strip().lower()
    if "@" not in addr:
        return ""
    domain = addr.rsplit("@", 1)[-1].strip().lstrip("www.")
    if not domain or domain in _GENERIC_EMAIL_DOMAINS:
        return ""
    return domain


def normalize_company_name(name: str | None) -> str:
    text = _COMPANY_SUFFIX_RE.sub(" ", name or "")
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _same_company(a: dict[str, Any], b: dict[str, Any]) -> bool:
    da = email_domain(a.get("contact_email"), a.get("organization_domain"))
    db_ = email_domain(b.get("contact_email"), b.get("organization_domain"))
    if da and db_ and da == db_:
        return True
    na = normalize_company_name(a.get("company_name"))
    nb = normalize_company_name(b.get("company_name"))
    if na and na == nb and len(na) >= 3:
        return True
    raw_a = (a.get("company_name") or "").strip().lower()
    raw_b = (b.get("company_name") or "").strip().lower()
    return bool(raw_a and raw_a == raw_b)


def outreach_peers_map(leads: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """For each lead, other people at the same company who were emailed or are in the queue."""
    if not leads:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, company_name, contact_name, contact_email, title, status, organization_domain
            FROM leads
            WHERE status IN ('drafted', 'approved', 'sending', 'sent', 'replied', 'closed')
            """
        ).fetchall()
    active = [dict(r) for r in rows]
    out: dict[int, list[dict[str, Any]]] = {int(lead["id"]): [] for lead in leads}
    for lead in leads:
        lid = int(lead["id"])
        for other in active:
            if int(other["id"]) == lid:
                continue
            if _same_company(lead, other):
                out[lid].append(other)
    return out


_CLUSTER_STATUSES = (
    "qualified",
    "drafted",
    "approved",
    "sending",
    "sent",
    "replied",
    "closed",
)


def _cluster_stable_key(members: list[dict[str, Any]]) -> str:
    domains = sorted(
        {
            email_domain(m.get("contact_email"), m.get("organization_domain"))
            for m in members
            if email_domain(m.get("contact_email"), m.get("organization_domain"))
        }
    )
    if domains:
        return "d:" + domains[0]
    names = sorted(
        {
            normalize_company_name(m.get("company_name"))
            for m in members
            if len(normalize_company_name(m.get("company_name"))) >= 3
        }
    )
    if names:
        return "n:" + names[0]
    ids = ",".join(str(int(m["id"])) for m in sorted(members, key=lambda x: int(x["id"])))
    return "ids:" + ids


def _group_company_clusters(people: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    unused = [dict(p) for p in people]
    clusters: list[list[dict[str, Any]]] = []
    while unused:
        seed = unused.pop(0)
        group = [seed]
        changed = True
        while changed:
            changed = False
            keep: list[dict[str, Any]] = []
            for other in unused:
                if any(_same_company(other, member) for member in group):
                    group.append(other)
                    changed = True
                else:
                    keep.append(other)
            unused = keep
        clusters.append(group)
    return clusters


def _next_company_code(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COUNT(*) AS n FROM company_codes").fetchone()
    return f"C-{(int(row['n']) if row else 0) + 1:02d}"


def _code_for_cluster_key(cluster_key: str) -> str:
    if not cluster_key:
        return ""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT code FROM company_codes WHERE cluster_key = ?",
            (cluster_key,),
        ).fetchone()
        if row:
            return str(row["code"])
        code = _next_company_code(conn)
        try:
            conn.execute(
                "INSERT INTO company_codes (cluster_key, code, created_at) VALUES (?, ?, ?)",
                (cluster_key, code, now_iso()),
            )
            return code
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT code FROM company_codes WHERE cluster_key = ?",
                (cluster_key,),
            ).fetchone()
            return str(row["code"]) if row else code


def load_cluster_people() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, company_name, contact_name, contact_email, title, status, organization_domain
            FROM leads
            WHERE status IN ({",".join("?" for _ in _CLUSTER_STATUSES)})
            """,
            _CLUSTER_STATUSES,
        ).fetchall()
        return [dict(r) for r in rows]


def company_code_map(leads: list[dict[str, Any]] | None = None) -> dict[int, str]:
    """Shared code for every person in a 2+ company cluster. Empty if the lead is alone."""
    people = load_cluster_people()
    extra = {int(x["id"]): dict(x) for x in (leads or [])}
    by_id = {int(p["id"]): p for p in people}
    by_id.update(extra)
    clusters = _group_company_clusters(list(by_id.values()))
    out: dict[int, str] = {}
    for group in clusters:
        if len(group) < 2:
            continue
        code = _code_for_cluster_key(_cluster_stable_key(group))
        for member in group:
            out[int(member["id"])] = code
    return out


def company_variant_index(lead_id: int) -> int:
    """0 = first person at the firm (by id), 1 = second, ... used to pick a different email angle."""
    people = load_cluster_people()
    lead = next((p for p in people if int(p["id"]) == int(lead_id)), None)
    if not lead:
        return 0
    group = [p for p in people if int(p["id"]) == int(lead_id) or _same_company(lead, p)]
    group.sort(key=lambda p: int(p["id"]))
    for i, member in enumerate(group):
        if int(member["id"]) == int(lead_id):
            return i
    return 0


def sibling_email_context(lead_id: int, kind: str | None = None) -> list[dict[str, Any]]:
    """Latest email text already written/sent to colleagues at the same company."""
    people = load_cluster_people()
    lead = next((p for p in people if int(p["id"]) == int(lead_id)), None)
    if not lead:
        return []
    siblings = [p for p in people if int(p["id"]) != int(lead_id) and _same_company(lead, p)]
    if not siblings:
        return []
    out: list[dict[str, Any]] = []
    with get_conn() as conn:
        for sib in siblings:
            sid = int(sib["id"])
            row = None
            if kind:
                row = conn.execute(
                    """
                    SELECT subject, body, kind FROM drafts
                    WHERE lead_id = ? AND IFNULL(kind, 'initial') = ?
                    ORDER BY version DESC, id DESC LIMIT 1
                    """,
                    (sid, kind),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT d.subject, d.body, d.kind
                    FROM sends s
                    JOIN drafts d ON d.id = s.draft_id
                    WHERE s.lead_id = ?
                    ORDER BY s.id DESC LIMIT 1
                    """,
                    (sid,),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT subject, body, kind FROM drafts
                    WHERE lead_id = ?
                    ORDER BY version DESC, id DESC LIMIT 1
                    """,
                    (sid,),
                ).fetchone()
            if not row:
                continue
            out.append(
                {
                    "id": sid,
                    "name": sib.get("contact_name") or "",
                    "title": sib.get("title") or "",
                    "subject": row["subject"] or "",
                    "body": row["body"] or "",
                    "kind": row["kind"] or "initial",
                }
            )
    return out


def next_draft_version(lead_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM drafts WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()
        return int(row["v"]) + 1


def insert_draft(
    lead_id: int,
    subject: str,
    body: str,
    hook_used: str,
    version: int,
    kind: str = "initial",
    subject_ar: str = "",
    body_ar: str = "",
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO drafts (lead_id, subject, body, hook_used, version, kind, subject_ar, body_ar)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (lead_id, subject, body, hook_used, version, kind, subject_ar or "", body_ar or ""),
        )
        return int(cur.lastrowid)


def update_draft(
    draft_id: int,
    subject: str,
    body: str,
    hook_used: str,
    subject_ar: str | None = None,
    body_ar: str | None = None,
    greeting: object = _UNSET,
    signoff: object = _UNSET,
    signature: object = _UNSET,
) -> None:
    sets = ["subject = ?", "body = ?", "hook_used = ?"]
    vals: list[Any] = [subject, body, hook_used]
    if subject_ar is not None or body_ar is not None:
        sets.append("subject_ar = COALESCE(?, subject_ar)")
        sets.append("body_ar = COALESCE(?, body_ar)")
        vals.extend([subject_ar, body_ar])
    if greeting is not _UNSET:
        sets.append("greeting = ?")
        vals.append(greeting)
    if signoff is not _UNSET:
        sets.append("signoff = ?")
        vals.append(signoff)
    if signature is not _UNSET:
        sets.append("signature = ?")
        vals.append(signature)
    vals.append(draft_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE drafts SET {', '.join(sets)} WHERE id = ?", vals)


def get_draft(draft_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return row_to_dict(row)


def get_latest_draft(lead_id: int, kind: str | None = None) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        if kind:
            row = conn.execute(
                """
                SELECT * FROM drafts
                WHERE lead_id = ? AND kind = ?
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (lead_id, kind),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM drafts
                WHERE lead_id = ?
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (lead_id,),
            ).fetchone()
        return row_to_dict(row)


def get_review_queue() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                l.*,
                d.id AS draft_id,
                d.subject,
                d.body,
                d.subject_ar,
                d.body_ar,
                d.hook_used,
                d.version,
                d.kind,
                d.greeting,
                d.signoff,
                d.signature
            FROM leads l
            JOIN drafts d ON d.id = (
                SELECT d2.id FROM drafts d2
                WHERE d2.lead_id = l.id AND IFNULL(d2.kind, 'initial') = 'initial'
                ORDER BY d2.version DESC, d2.id DESC
                LIMIT 1
            )
            WHERE l.status = 'drafted'
            ORDER BY l.fit_score DESC, l.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_today_send_count() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count FROM daily_send_log WHERE date = ?",
            (today_iso(),),
        ).fetchone()
        return int(row["count"]) if row else 0


def increment_daily_send_log() -> int:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO daily_send_log (date, count) VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET count = count + 1
            """,
            (today_iso(),),
        )
        row = conn.execute(
            "SELECT count FROM daily_send_log WHERE date = ?",
            (today_iso(),),
        ).fetchone()
        return int(row["count"])


def reserve_daily_slot(cap: int) -> bool:
    """Count today's send. No daily cap — always succeeds."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO daily_send_log (date, count) VALUES (?, 0)",
            (today_iso(),),
        )
        conn.execute(
            "UPDATE daily_send_log SET count = count + 1 WHERE date = ?",
            (today_iso(),),
        )
        return True


def release_daily_slot() -> None:
    """Give back a slot when the SMTP send failed after reserving."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE daily_send_log SET count = MAX(count - 1, 0) WHERE date = ?",
            (today_iso(),),
        )


def get_recent_sent_emails(n: int = 3, kind: str = "initial") -> list[dict[str, Any]]:
    """Latest actually-sent emails of one kind, newest first. Text lives on drafts."""
    n = max(1, min(20, int(n)))
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                d.subject,
                d.body,
                s.sent_at,
                s.kind,
                l.company_name,
                l.contact_name
            FROM sends s
            JOIN drafts d ON d.id = s.draft_id
            JOIN leads l ON l.id = s.lead_id
            WHERE IFNULL(s.kind, 'initial') = ?
            ORDER BY s.sent_at DESC, s.id DESC
            LIMIT ?
            """,
            (kind, n),
        ).fetchall()
        return [dict(r) for r in rows]


def insert_send(draft_id: int, lead_id: int, kind: str = "initial", message_id: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sends (draft_id, lead_id, sent_at, replied, outcome, kind, message_id)
            VALUES (?, ?, ?, 0, 'no_response', ?, ?)
            """,
            (draft_id, lead_id, now_iso(), kind, message_id),
        )
        conn.execute(
            "UPDATE leads SET last_contacted_at = ? WHERE id = ?",
            (now_iso(), lead_id),
        )
        return int(cur.lastrowid)


def has_send(lead_id: int, kind: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM sends
            WHERE lead_id = ? AND IFNULL(kind, 'initial') = ?
            LIMIT 1
            """,
            (lead_id, kind),
        ).fetchone()
        return row is not None


def get_tracker_rows() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id AS send_id,
                s.sent_at,
                s.replied,
                s.replied_at,
                s.outcome,
                s.kind AS send_kind,
                l.id AS lead_id,
                l.company_name,
                l.contact_name,
                l.contact_email,
                l.title,
                l.status AS lead_status,
                l.fit_score,
                l.fit_reasoning,
                l.linkedin_url,
                l.outcome_note,
                l.outcome_note_at,
                d.subject,
                d.body,
                d.subject_ar,
                d.body_ar,
                d.hook_used,
                d.greeting,
                d.signoff,
                d.signature
            FROM sends s
            JOIN leads l ON l.id = s.lead_id
            JOIN drafts d ON d.id = s.draft_id
            ORDER BY s.id DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def _anchor_for_followup(
    lead_id: int, step: int, conn: sqlite3.Connection | None = None
) -> datetime | None:
    """Timestamp the follow-up should count from: initial send, or the previous step's send."""

    def _lookup(c: sqlite3.Connection) -> datetime | None:
        if int(step) <= 1:
            row = c.execute(
                """
                SELECT sent_at FROM sends
                WHERE lead_id = ? AND IFNULL(kind, 'initial') = 'initial'
                ORDER BY id ASC LIMIT 1
                """,
                (lead_id,),
            ).fetchone()
        else:
            kind = f"followup_{int(step) - 1}"
            row = c.execute(
                """
                SELECT sent_at FROM sends
                WHERE lead_id = ? AND kind = ?
                ORDER BY id DESC LIMIT 1
                """,
                (lead_id, kind),
            ).fetchone()
            if row is None:
                row = c.execute(
                    """
                    SELECT sent_at FROM follow_ups
                    WHERE lead_id = ? AND step = ? AND status = 'sent'
                    ORDER BY id DESC LIMIT 1
                    """,
                    (lead_id, int(step) - 1),
                ).fetchone()
        return parse_when(row["sent_at"]) if row else None

    if conn is not None:
        return _lookup(conn)
    with get_conn() as owned:
        return _lookup(owned)


def schedule_followup(lead_id: int, step: int, days: int, start: datetime | None = None) -> int:
    origin = start or _anchor_for_followup(lead_id, step)
    when = _followup_moment(days, start=origin)
    return _upsert_followup(lead_id, step, when)


def schedule_followup_soon(lead_id: int, step: int) -> int:
    """Schedule a follow-up for the next sendable slot (used for the video email)."""
    return _upsert_followup(lead_id, step, _soon_moment())


def pull_pending_video_followups() -> int:
    """Video follow-up is retired. Cancel any unsent step-3 rows."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE follow_ups
            SET status = 'cancelled',
                last_error = 'اتلغت: متابعة الفيديو اتقفلت',
                claim_token = NULL,
                claimed_at = NULL
            WHERE step = 3 AND status IN ('scheduled', 'processing', 'ready')
            """
        )
        return int(cur.rowcount or 0)


def reset_unsent_followup_drafts(step: int) -> int:
    """Drop unsent drafts for one follow-up step so they are written again."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id FROM follow_ups
            WHERE step = ? AND status IN ('ready', 'processing', 'failed')
            """,
            (int(step),),
        ).fetchall()
    n = 0
    for row in rows:
        if restore_followup_to_schedule(int(row["id"])):
            n += 1
    return n


def _upsert_followup(lead_id: int, step: int, when: datetime) -> int:
    when_s = when.strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        live = conn.execute(
            """
            SELECT id, status FROM follow_ups
            WHERE lead_id = ? AND step = ?
              AND status IN ('scheduled', 'processing', 'ready', 'sending', 'sent')
            ORDER BY id DESC LIMIT 1
            """,
            (lead_id, step),
        ).fetchone()
        if live:
            if live["status"] in {"sent", "sending", "ready"}:
                return int(live["id"])
            conn.execute(
                """
                UPDATE follow_ups
                SET scheduled_at = ?, status = 'scheduled', last_error = NULL,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = ?
                """,
                (when_s, int(live["id"])),
            )
            return int(live["id"])
        revived = conn.execute(
            """
            SELECT id FROM follow_ups
            WHERE lead_id = ? AND step = ? AND status = 'cancelled'
              AND last_error = ?
            ORDER BY id DESC LIMIT 1
            """,
            (lead_id, step, _SETTINGS_CANCEL_MARK),
        ).fetchone()
        if revived:
            conn.execute(
                """
                UPDATE follow_ups
                SET scheduled_at = ?, status = 'scheduled', last_error = NULL,
                    claim_token = NULL, claimed_at = NULL
                WHERE id = ?
                """,
                (when_s, int(revived["id"])),
            )
            return int(revived["id"])
        cur = conn.execute(
            """
            INSERT INTO follow_ups (lead_id, step, scheduled_at, status)
            VALUES (?, ?, ?, 'scheduled')
            """,
            (lead_id, step, when_s),
        )
        return int(cur.lastrowid)


def cancel_followups(lead_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE follow_ups
            SET status = 'cancelled', claim_token = NULL, claimed_at = NULL
            WHERE lead_id = ? AND status IN ('scheduled', 'processing', 'ready', 'sending')
            """,
            (lead_id,),
        )


def apply_inflight_followups(settings: dict[str, Any]) -> dict[str, int]:
    """Re-apply follow-up settings to rows that have not been sent yet."""
    enabled = bool(settings.get("followup_enabled"))
    max_n = int(settings.get("max_followups") or 0)
    days = {
        1: int(settings.get("followup_1_days") or 3),
        2: int(settings.get("followup_2_days") or 4),
        3: int(settings.get("followup_3_days") or 2),
    }
    moved = cancelled = created = 0
    with get_conn() as conn:
        if not enabled or max_n < 1:
            cur = conn.execute(
                """
                UPDATE follow_ups
                SET status = 'cancelled',
                    last_error = ?,
                    claim_token = NULL,
                    claimed_at = NULL
                WHERE status IN ('scheduled', 'processing', 'ready')
                """,
                (_SETTINGS_CANCEL_MARK,),
            )
            return {"fu_moved": 0, "fu_cancelled": int(cur.rowcount or 0), "fu_created": 0}

        if max_n < 3:
            cur = conn.execute(
                """
                UPDATE follow_ups
                SET status = 'cancelled',
                    last_error = ?,
                    claim_token = NULL,
                    claimed_at = NULL
                WHERE step >= 3 AND status IN ('scheduled', 'processing', 'ready')
                """,
                (_SETTINGS_CANCEL_MARK,),
            )
            cancelled += int(cur.rowcount or 0)

        if max_n < 2:
            cur = conn.execute(
                """
                UPDATE follow_ups
                SET status = 'cancelled',
                    last_error = ?,
                    claim_token = NULL,
                    claimed_at = NULL
                WHERE step >= 2 AND status IN ('scheduled', 'processing', 'ready')
                """,
                (_SETTINGS_CANCEL_MARK,),
            )
            cancelled += int(cur.rowcount or 0)

        rows = conn.execute(
            """
            SELECT f.id, f.lead_id, f.step, f.status, f.scheduled_at, l.status AS lead_status
            FROM follow_ups f
            JOIN leads l ON l.id = f.lead_id
            WHERE f.status IN ('scheduled', 'processing', 'ready')
              AND f.step <= ?
            """,
            (max_n,),
        ).fetchall()

        for row in rows:
            if row["lead_status"] in {"replied", "closed", "bounced"}:
                conn.execute(
                    """
                    UPDATE follow_ups
                    SET status = 'cancelled', last_error = ?, claim_token = NULL, claimed_at = NULL
                    WHERE id = ?
                    """,
                    (f"اتلغت: حالة الليدر {row['lead_status']}", int(row["id"])),
                )
                cancelled += 1
                continue
            step = int(row["step"])
            if step == 3:
                continue
            start = _anchor_for_followup(int(row["lead_id"]), step, conn=conn)
            when = _followup_moment(days.get(step, days[1]), start=start).strftime("%Y-%m-%d %H:%M:%S")
            keep = "ready" if row["status"] == "ready" else "scheduled"
            conn.execute(
                """
                UPDATE follow_ups
                SET scheduled_at = ?,
                    status = ?,
                    last_error = 'اتظبطت من الإعدادات',
                    claim_token = NULL,
                    claimed_at = NULL
                WHERE id = ?
                """,
                (when, keep, int(row["id"])),
            )
            moved += 1

        if max_n >= 1:
            missing1 = conn.execute(
                """
                SELECT l.id
                FROM leads l
                WHERE l.status = 'sent'
                  AND EXISTS (
                      SELECT 1 FROM sends s
                      WHERE s.lead_id = l.id AND IFNULL(s.kind, 'initial') = 'initial'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM follow_ups f
                      WHERE f.lead_id = l.id AND f.step = 1
                        AND (
                            f.status IN ('scheduled', 'processing', 'ready', 'sending', 'sent')
                            OR (
                                f.status = 'cancelled'
                                AND IFNULL(f.last_error, '') != ?
                            )
                        )
                  )
                """,
                (_SETTINGS_CANCEL_MARK,),
            ).fetchall()
        else:
            missing1 = []
        if max_n >= 2:
            missing2 = conn.execute(
                """
                SELECT l.id
                FROM leads l
                WHERE l.status = 'sent'
                  AND EXISTS (
                      SELECT 1 FROM follow_ups f1
                      WHERE f1.lead_id = l.id AND f1.step = 1 AND f1.status = 'sent'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM follow_ups f2
                      WHERE f2.lead_id = l.id AND f2.step = 2
                        AND (
                            f2.status IN ('scheduled', 'processing', 'ready', 'sending', 'sent')
                            OR (
                                f2.status = 'cancelled'
                                AND IFNULL(f2.last_error, '') != ?
                            )
                        )
                  )
                """,
                (_SETTINGS_CANCEL_MARK,),
            ).fetchall()
        else:
            missing2 = []
        if max_n >= 3:
            missing3 = conn.execute(
                """
                SELECT l.id
                FROM leads l
                WHERE l.status = 'sent'
                  AND EXISTS (
                      SELECT 1 FROM follow_ups f2
                      WHERE f2.lead_id = l.id AND f2.step = 2 AND f2.status = 'sent'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM follow_ups f3
                      WHERE f3.lead_id = l.id AND f3.step = 3
                        AND (
                            f3.status IN ('scheduled', 'processing', 'ready', 'sending', 'sent')
                            OR (
                                f3.status = 'cancelled'
                                AND IFNULL(f3.last_error, '') != ?
                            )
                        )
                  )
                """,
                (_SETTINGS_CANCEL_MARK,),
            ).fetchall()
        else:
            missing3 = []

    for row in missing1:
        schedule_followup(int(row["id"]), 1, days[1])
        created += 1
    for row in missing2:
        schedule_followup(int(row["id"]), 2, days[2])
        created += 1
    for row in missing3:
        schedule_followup_soon(int(row["id"]), 3)
        created += 1
    return {"fu_moved": moved, "fu_cancelled": cancelled, "fu_created": created}


def apply_inflight_threshold(min_score: int) -> dict[str, int]:
    """Re-bucket unsent leads against the current qualify threshold. Manual rejects stay."""
    min_score = int(min_score)
    demoted = promoted = 0
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE leads
            SET status = 'rejected',
                reject_reason = 'تحت حد القبول بعد تعديل الإعدادات',
                reject_source = COALESCE(reject_source, 'llm'),
                rejected_at = COALESCE(rejected_at, ?)
            WHERE status = 'qualified'
              AND fit_score IS NOT NULL
              AND fit_score < ?
            """,
            (now_iso(), min_score),
        )
        demoted = int(cur.rowcount or 0)
        cur = conn.execute(
            """
            UPDATE leads
            SET status = 'qualified',
                reject_reason = NULL,
                reject_source = NULL,
                rejected_at = NULL
            WHERE status = 'rejected'
              AND IFNULL(reject_source, 'llm') IN ('llm', 'rule')
              AND fit_score IS NOT NULL
              AND fit_score >= ?
            """,
            (min_score,),
        )
        promoted = int(cur.rowcount or 0)
    return {"demoted": demoted, "requalified": promoted}


_FOLLOWUP_SELECT = """
            SELECT
                f.*,
                l.company_name,
                l.contact_name,
                l.contact_email,
                l.title,
                l.suggested_hook,
                l.status AS lead_status
            FROM follow_ups f
            JOIN leads l ON l.id = f.lead_id
"""


def get_due_followups() -> list[dict[str, Any]]:
    """Read-only view of rows that would be claimed. Does not lock them."""
    if is_weekend():
        return []
    with get_conn() as conn:
        rows = conn.execute(
            _FOLLOWUP_SELECT
            + """
            WHERE f.status = 'scheduled'
              AND date(f.scheduled_at) <= ?
              AND l.status IN ('sent', 'approved')
            ORDER BY f.scheduled_at ASC
            """,
            (today_iso(),),
        ).fetchall()
        return [dict(r) for r in rows]


def defer_weekend_due() -> int:
    """Weekend guard: anything due now moves to the next weekday, same clock."""
    if not is_weekend():
        return 0
    moved = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, scheduled_at FROM follow_ups WHERE status = 'scheduled' AND date(scheduled_at) <= ?",
            (today_iso(),),
        ).fetchall()
        for row in rows:
            try:
                when = datetime.strptime(str(row["scheduled_at"]), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                when = datetime.now()
            new_when = shift_off_weekend(max(when, datetime.now()))
            conn.execute(
                "UPDATE follow_ups SET scheduled_at = ?, last_error = ? WHERE id = ?",
                (new_when.strftime("%Y-%m-%d %H:%M:%S"), "اتأجلت للأسبوع الجاي (ويكند)", int(row["id"])),
            )
            moved += 1
    return moved


def claim_due_followups(limit: int = 5, stale_minutes: int = 2) -> list[dict[str, Any]]:
    """Atomically mark due rows as processing so two workers cannot send the same one."""
    if is_weekend():
        defer_weekend_due()
        return []
    token = uuid.uuid4().hex
    now = now_iso()
    cutoff = (datetime.now() - timedelta(minutes=max(1, stale_minutes))).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE follow_ups
            SET status = 'scheduled', claim_token = NULL, claimed_at = NULL
            WHERE status = 'processing'
              AND (claimed_at IS NULL OR claimed_at < ?)
            """,
            (cutoff,),
        )
        conn.execute(
            """
            UPDATE follow_ups
            SET status = 'ready', claim_token = NULL, claimed_at = NULL
            WHERE status = 'sending'
              AND (claimed_at IS NULL OR claimed_at < ?)
              AND NOT EXISTS (
                  SELECT 1 FROM sends s
                  WHERE s.lead_id = follow_ups.lead_id
                    AND s.kind = 'followup_' || follow_ups.step
              )
            """,
            (cutoff,),
        )
        conn.execute(
            """
            UPDATE follow_ups
            SET status = 'processing',
                claimed_at = ?,
                claim_token = ?,
                last_error = NULL
            WHERE id IN (
                SELECT f.id
                FROM follow_ups f
                JOIN leads l ON l.id = f.lead_id
                WHERE f.status = 'scheduled'
                  AND date(f.scheduled_at) <= ?
                  AND l.status IN ('sent', 'approved')
                ORDER BY f.scheduled_at ASC
                LIMIT ?
            )
            """,
            (now, token, today_iso(), max(1, int(limit))),
        )
        rows = conn.execute(
            _FOLLOWUP_SELECT + " WHERE f.claim_token = ? ORDER BY f.scheduled_at ASC",
            (token,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_followup_queue() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                f.*,
                l.company_name,
                l.contact_name,
                l.contact_email,
                l.title,
                l.linkedin_url,
                (
                    SELECT s.sent_at FROM sends s
                    WHERE s.lead_id = f.lead_id AND IFNULL(s.kind, 'initial') = 'initial'
                    ORDER BY s.id ASC LIMIT 1
                ) AS initial_sent_at,
                (
                    SELECT s.id FROM sends s
                    WHERE s.lead_id = f.lead_id AND IFNULL(s.kind, 'initial') = 'initial'
                    ORDER BY s.id ASC LIMIT 1
                ) AS initial_send_id,
                (
                    SELECT s.outcome FROM sends s
                    WHERE s.lead_id = f.lead_id AND IFNULL(s.kind, 'initial') = 'initial'
                    ORDER BY s.id ASC LIMIT 1
                ) AS initial_outcome,
                (
                    SELECT s.id FROM sends s
                    WHERE s.lead_id = f.lead_id AND s.kind = 'followup_1'
                    ORDER BY s.id DESC LIMIT 1
                ) AS fu1_send_id,
                (
                    SELECT s.sent_at FROM sends s
                    WHERE s.lead_id = f.lead_id AND s.kind = 'followup_1'
                    ORDER BY s.id DESC LIMIT 1
                ) AS fu1_sent_at,
                (
                    SELECT s.outcome FROM sends s
                    WHERE s.lead_id = f.lead_id AND s.kind = 'followup_1'
                    ORDER BY s.id DESC LIMIT 1
                ) AS fu1_outcome,
                (
                    SELECT s.id FROM sends s
                    WHERE s.lead_id = f.lead_id AND s.kind = 'followup_2'
                    ORDER BY s.id DESC LIMIT 1
                ) AS fu2_send_id,
                (
                    SELECT s.outcome FROM sends s
                    WHERE s.lead_id = f.lead_id AND s.kind = 'followup_2'
                    ORDER BY s.id DESC LIMIT 1
                ) AS fu2_outcome
            FROM follow_ups f
            JOIN leads l ON l.id = f.lead_id
            ORDER BY
                CASE f.status
                    WHEN 'ready' THEN 0
                    WHEN 'sending' THEN 0
                    WHEN 'processing' THEN 1
                    WHEN 'scheduled' THEN 2
                    WHEN 'failed' THEN 3
                    ELSE 4
                END,
                f.scheduled_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def count_followups_ready() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM follow_ups WHERE status IN ('ready', 'sending')"
        ).fetchone()
        return int(row["n"] or 0)


def get_followup_review_queue() -> list[dict[str, Any]]:
    """Follow-ups that have a draft and are waiting for human approve-and-send."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                l.id,
                l.company_name,
                l.contact_name,
                l.contact_email,
                l.title,
                l.fit_score,
                l.fit_reasoning,
                l.source_notes,
                l.suggested_hook,
                l.organization_domain,
                l.linkedin_url,
                l.status AS lead_status,
                f.id AS followup_id,
                f.step,
                f.scheduled_at,
                f.status AS followup_status,
                d.id AS draft_id,
                d.subject,
                d.body,
                d.subject_ar,
                d.body_ar,
                d.hook_used,
                d.version,
                d.kind,
                d.greeting,
                d.signoff,
                d.signature
            FROM follow_ups f
            JOIN leads l ON l.id = f.lead_id
            JOIN drafts d ON d.id = COALESCE(
                f.draft_id,
                (
                    SELECT d2.id FROM drafts d2
                    WHERE d2.lead_id = f.lead_id
                      AND d2.kind = 'followup_' || f.step
                    ORDER BY d2.version DESC, d2.id DESC
                    LIMIT 1
                )
            )
            WHERE f.status IN ('ready', 'sending')
              AND l.status IN ('sent', 'approved')
            ORDER BY f.scheduled_at ASC, f.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def claim_followup_for_send(followup_id: int) -> bool:
    """Exclusive lock so two clicks cannot send the same follow-up."""
    stamp = now_iso()
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE follow_ups
            SET status = 'sending', claimed_at = ?, claim_token = NULL, last_error = NULL
            WHERE id = ?
              AND status = 'ready'
              AND NOT EXISTS (
                  SELECT 1 FROM sends s
                  WHERE s.lead_id = follow_ups.lead_id
                    AND s.kind = 'followup_' || follow_ups.step
              )
            """,
            (stamp, followup_id),
        )
        return cur.rowcount == 1


def unclaim_followup_after_failed_send(followup_id: int, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE follow_ups
            SET status = 'ready',
                last_error = COALESCE(?, last_error),
                claim_token = NULL,
                claimed_at = NULL
            WHERE id = ?
              AND status = 'sending'
            """,
            (error, followup_id),
        )


def restore_followup_to_schedule(followup_id: int) -> bool:
    """Put a generated (unsent) follow-up back on the calendar. Draft is dropped so it is written again when due."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT lead_id, step, status FROM follow_ups WHERE id = ?",
            (int(followup_id),),
        ).fetchone()
        if not row or row["status"] not in {"ready", "processing", "failed"}:
            return False
        kind = f"followup_{int(row['step'])}"
        lid = int(row["lead_id"])
        sent = conn.execute(
            """
            SELECT 1 FROM sends
            WHERE lead_id = ? AND IFNULL(kind, 'initial') = ?
            LIMIT 1
            """,
            (lid, kind),
        ).fetchone()
        if sent:
            return False
        conn.execute(
            "DELETE FROM drafts WHERE lead_id = ? AND IFNULL(kind, 'initial') = ?",
            (lid, kind),
        )
        conn.execute(
            """
            UPDATE follow_ups
            SET status = 'scheduled',
                draft_id = NULL,
                last_error = NULL,
                claim_token = NULL,
                claimed_at = NULL
            WHERE id = ?
            """,
            (int(followup_id),),
        )
        return True


def restore_ready_followups() -> int:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM follow_ups WHERE status IN ('ready', 'processing')"
        ).fetchall()
    n = 0
    for row in rows:
        if restore_followup_to_schedule(int(row["id"])):
            n += 1
    return n


def release_processing_followups() -> int:
    """Abandon leftover 'processing' locks so a new تجهيز المستحق can proceed."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE follow_ups
            SET status = 'scheduled',
                claim_token = NULL,
                claimed_at = NULL,
                last_error = COALESCE(last_error, 'اتفك من تجهيز متقطع')
            WHERE status = 'processing'
            """
        )
        return int(cur.rowcount or 0)


def mark_followup(followup_id: int, status: str, draft_id: int | None = None, error: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE follow_ups
            SET status = ?,
                draft_id = COALESCE(?, draft_id),
                sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END,
                last_error = ?,
                claim_token = NULL,
                claimed_at = NULL
            WHERE id = ?
            """,
            (status, draft_id, status, now_iso(), error, followup_id),
        )


def release_followup(followup_id: int, error: str | None = None) -> None:
    """Return a claimed row to scheduled without moving the due time."""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE follow_ups
            SET status = 'scheduled',
                last_error = COALESCE(?, last_error),
                claim_token = NULL,
                claimed_at = NULL
            WHERE id = ? AND status = 'processing'
            """,
            (error, followup_id),
        )


def reschedule_followup(followup_id: int, when: datetime, error: str | None = None) -> None:
    stamp = shift_off_weekend(when).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE follow_ups
            SET status = 'scheduled',
                scheduled_at = ?,
                last_error = ?,
                claim_token = NULL,
                claimed_at = NULL
            WHERE id = ?
            """,
            (stamp, error, followup_id),
        )


def retry_followup(followup_id: int) -> None:
    """Manual retry: due at the next weekday now."""
    reschedule_followup(followup_id, datetime.now(), error=None)


def thread_message_ids(lead_id: int) -> list[str]:
    """Message-IDs already sent on this lead, oldest first. Skips rows with no id."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT message_id
            FROM sends
            WHERE lead_id = ? AND message_id IS NOT NULL AND TRIM(message_id) != ''
            ORDER BY id ASC
            """,
            (lead_id,),
        ).fetchall()
    return [str(row["message_id"]).strip() for row in rows if str(row["message_id"] or "").strip()]


def initial_send_for_lead(lead_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT s.*, d.subject, d.body, d.hook_used, d.greeting, d.signoff, d.signature
            FROM sends s
            JOIN drafts d ON d.id = s.draft_id
            WHERE s.lead_id = ? AND IFNULL(s.kind, 'initial') = 'initial'
            ORDER BY s.id ASC
            LIMIT 1
            """,
            (lead_id,),
        ).fetchone()
        return row_to_dict(row)


def get_sent_thread(lead_id: int) -> list[dict[str, Any]]:
    """Every sent email in this lead's thread, oldest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                s.id AS send_id,
                s.kind,
                s.sent_at,
                s.outcome,
                d.subject,
                d.body,
                d.subject_ar,
                d.body_ar,
                d.hook_used,
                d.greeting,
                d.signoff,
                d.signature
            FROM sends s
            JOIN drafts d ON d.id = s.draft_id
            WHERE s.lead_id = ?
            ORDER BY s.id ASC
            """,
            (lead_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_outcome_note(lead_id: int, note: str) -> None:
    text = (note or "").strip() or None
    with get_conn() as conn:
        conn.execute(
            "UPDATE leads SET outcome_note = ?, outcome_note_at = ? WHERE id = ?",
            (text, now_iso() if text else None, lead_id),
        )


def mark_outcome(
    lead_id: int,
    outcome: str,
    replied: bool,
    send_id: int | None = None,
) -> Optional[dict[str, Any]]:
    """Record an outcome on a specific send (or the latest one) and update the lead.

    `no_response` is a no-op on follow-ups: it only records the label and keeps the
    lead in `sent` so scheduled follow-ups continue.
    """
    status_map = {
        "replied": "replied",
        "positive": "replied",
        "negative": "replied",
        "meeting_booked": "replied",
        "closed_won": "closed",
        "closed_lost": "closed",
        "bounced": "bounced",
        "no_response": "sent",
    }
    lead_status = status_map.get(outcome, "sent")
    replied_at = now_iso() if replied else None
    with get_conn() as conn:
        if send_id is None:
            row = conn.execute(
                "SELECT id FROM sends WHERE lead_id = ? ORDER BY id DESC LIMIT 1", (lead_id,)
            ).fetchone()
            send_id = int(row["id"]) if row else None
        if send_id is not None:
            conn.execute(
                """
                UPDATE sends
                SET outcome = ?,
                    replied = ?,
                    replied_at = COALESCE(?, replied_at)
                WHERE id = ? AND lead_id = ?
                """,
                (outcome, 1 if replied else 0, replied_at, send_id, lead_id),
            )
        conn.execute("UPDATE leads SET status = ? WHERE id = ?", (lead_status, lead_id))
        if replied or outcome in {"closed_won", "closed_lost", "negative", "bounced"}:
            conn.execute(
                """
                UPDATE follow_ups
                SET status = 'cancelled', claim_token = NULL, claimed_at = NULL,
                    last_error = ?
                WHERE lead_id = ? AND status IN ('scheduled', 'processing', 'ready', 'sending')
                """,
                (f"اتلغت: النتيجة {outcome}", lead_id),
            )
        row = conn.execute(
            """
            SELECT
                s.id AS send_id,
                l.id AS lead_id,
                l.company_name,
                d.hook_used,
                d.body,
                d.subject,
                s.outcome
            FROM sends s
            JOIN leads l ON l.id = s.lead_id
            JOIN drafts d ON d.id = s.draft_id
            WHERE s.id = ?
            LIMIT 1
            """,
            (send_id,),
        ).fetchone()
        return row_to_dict(row)


def refresh_unsent_drafts(cleaner, extra_strip: list[str] | None = None) -> int:
    """Re-strip greeting/sign-off/old signature from the latest draft of every unsent item."""
    changed = 0
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.body, l.contact_name
            FROM drafts d
            JOIN leads l ON l.id = d.lead_id
            WHERE (
                l.status IN ('drafted', 'approved', 'qualified')
                OR EXISTS (
                    SELECT 1 FROM follow_ups f
                    WHERE f.draft_id = d.id
                      AND f.status IN ('scheduled', 'processing', 'ready')
                )
            )
              AND d.id = (
                  SELECT d2.id FROM drafts d2
                  WHERE d2.lead_id = d.lead_id
                    AND IFNULL(d2.kind, 'initial') = IFNULL(d.kind, 'initial')
                  ORDER BY d2.id DESC
                  LIMIT 1
              )
            """
        ).fetchall()
        for row in rows:
            old = row["body"] or ""
            new_body = cleaner(old, row["contact_name"] or "", extra_strip)
            if new_body != old:
                conn.execute("UPDATE drafts SET body = ? WHERE id = ?", (new_body, int(row["id"])))
                changed += 1
    return changed


def sanitize_unsent_drafts(cleaner) -> int:
    """One-time cleanup on first boot after greeting/signature became settings-controlled."""
    with get_conn() as conn:
        if _meta_get(conn, "mig_strip_greeting_signoff") is not None:
            return 0
    changed = refresh_unsent_drafts(cleaner)
    with get_conn() as conn:
        _meta_set(conn, "mig_strip_greeting_signoff", str(changed))
    return changed
