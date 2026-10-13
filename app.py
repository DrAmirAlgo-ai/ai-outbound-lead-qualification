"""AI Outbound — institutional outbound command surface."""

from __future__ import annotations

import html
import importlib
import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path

import streamlit as st

import csv_import
import db
import emailer
import i18n
import jobs
import llm
import llm_keys
import scheduler
import sending
import settings_store


def _reload_if_changed(mod):
    path = Path(getattr(mod, "__file__", "") or "")
    if not path.is_file():
        return mod
    mtime = path.stat().st_mtime_ns
    if getattr(mod, "_zq_mtime", None) == mtime:
        return mod
    fresh = importlib.reload(mod)
    fresh._zq_mtime = mtime
    return fresh


settings_store = _reload_if_changed(settings_store)
llm = _reload_if_changed(llm)
db = _reload_if_changed(db)
sending = _reload_if_changed(sending)
llm_keys = _reload_if_changed(llm_keys)
csv_import = _reload_if_changed(csv_import)
i18n = _reload_if_changed(i18n)
from settings_store import (
    DEFAULT_DRAFT_PROMPT,
    DEFAULT_FOLLOWUP_2_PROMPT,
    DEFAULT_FOLLOWUP_3_PROMPT,
    DEFAULT_FOLLOWUP_PROMPT,
    DEFAULT_ICP,
    DEFAULT_QUALIFY_PROMPT,
    env_get,
    load_env,
    load_settings,
    migrate_prompts,
    save_settings,
    settings_location,
    upsert_env,
)

st.set_page_config(
    page_title="AI Outbound",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="collapsed",
)

db.init_db()
load_env()


@st.cache_resource
def _boot_worker() -> bool:
    db.sanitize_unsent_drafts(llm.clean_llm_body)
    migrate_prompts()
    if hasattr(db, "pull_pending_video_followups"):
        db.pull_pending_video_followups()
    scheduler.start_background()
    return True


_boot_worker()

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Serif:wght@600&display=swap');

:root {
  --paper: #f4f0e8;
  --card: #fffcf7;
  --ink: #1c1915;
  --muted: #6f675c;
  --line: #e6dfd4;
  --gold: #6b5428;
  --gold-soft: #f4ead4;
  --good: #215e3b;
  --good-bg: #e7f3ea;
  --mid: #8a6a16;
  --mid-bg: #f7efd4;
  --low: #8a2e2e;
  --low-bg: #f8e8e6;
}

html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] {
    background: var(--paper);
    color: var(--ink);
    font-family: "IBM Plex Sans", sans-serif;
}
[data-testid="stHeader"] { background: var(--paper) !important; }
.block-container { padding-top: 3.6rem !important; padding-bottom: 3rem; max-width: 1180px; }
h1, h2, h3, h4, h5 { font-family: "IBM Plex Serif", serif; color: var(--ink) !important; letter-spacing: -0.02em; }
[data-testid="stToolbar"] { background: transparent; }
footer { visibility: hidden; }

[data-testid="stMetric"] {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 10px 12px;
}
[data-testid="stMetricLabel"] { color: var(--muted) !important; font-size: 0.78rem !important; font-weight: 500 !important; }
[data-testid="stMetricValue"] { color: var(--ink) !important; font-size: 1.4rem !important; font-weight: 600 !important; }

div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
    background: var(--card);
    border: 1px solid var(--line) !important;
    border-radius: 14px;
    padding: 4px 2px;
}

.stButton > button {
    border-radius: 8px;
    font-weight: 500;
    border-color: var(--line);
    color: var(--ink);
    background: var(--card);
}
.stButton > button[kind="primary"] {
    background: var(--ink);
    color: var(--gold-soft);
    border: 1px solid var(--ink);
}
.stButton > button[kind="primary"]:hover {
    background: var(--gold);
    border-color: var(--gold);
    color: #fff;
}

textarea, [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input, [data-testid="stSelectbox"] div {
    color: var(--ink) !important;
    background: #fff !important;
}
textarea, [data-testid="stTextInput"] input, [data-testid="stNumberInput"] input {
    direction: ltr;
    text-align: left;
}

.brand-kicker {
    color: var(--gold);
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    margin-bottom: 0.15rem;
}
.brand-title { font-family: "IBM Plex Serif", serif; font-size: 1.55rem; margin: 0; color: var(--ink); line-height: 1.15; }
.engine-bar {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px 16px;
    margin: 0.75rem 0 0.85rem;
}
.engine-meta { color: var(--muted); font-size: 0.82rem; line-height: 1.55; }
.engine-meta strong { color: var(--ink); font-weight: 600; }
.quota-track { height: 4px; background: #ece6db; border-radius: 999px; margin-top: 8px; overflow: hidden; }
.quota-fill { height: 100%; background: var(--gold); }

.section-title {
    font-family: "IBM Plex Serif", serif;
    font-size: 1.02rem;
    font-weight: 600;
    margin: 0 0 0.75rem;
    color: var(--ink);
}

.lead-card { padding: 0 0 4px; margin: 0; }
.lead-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.lead-name { font-family: "IBM Plex Serif", serif; font-size: 1.08rem; font-weight: 600; color: var(--ink); line-height: 1.25; }
.lead-co { font-size: 0.92rem; font-weight: 500; color: var(--ink); margin-top: 2px; }
.lead-meta { color: var(--muted); font-size: 0.8rem; margin-top: 4px; direction: ltr; text-align: left; }
.lead-badges { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; align-items: center; }
.meta-link { font-size: 0.78rem; direction: ltr; text-align: left; margin: 6px 0 2px; word-break: break-all; }
.meta-link a { color: var(--gold); text-decoration: none; }
.meta-link a:hover { text-decoration: underline; }

.badge {
    display: inline-block;
    font-size: 0.7rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    padding: 3px 8px;
    border-radius: 999px;
    line-height: 1.3;
    white-space: nowrap;
}
.badge-good { background: var(--good-bg); color: var(--good); }
.badge-mid { background: var(--mid-bg); color: var(--mid); }
.badge-low { background: var(--low-bg); color: var(--low); }
.badge-muted { background: #ece8e1; color: #5c574f; }
.badge-gold { background: var(--gold-soft); color: var(--gold); }

.panel {
    background: #fff;
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px 14px;
    font-size: 0.9rem;
    line-height: 1.6;
    white-space: pre-wrap;
    direction: ltr;
    text-align: left;
    color: var(--ink);
    margin: 0 0 8px;
}
.panel-ar { direction: rtl; text-align: right; }

.empty { color: var(--muted); font-size: 0.9rem; padding: 28px 8px; text-align: center; }

.map-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin: 8px 0 4px; }
.map-cell { background: #fff; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.map-label { color: var(--muted); font-size: 0.68rem; letter-spacing: 0.06em; text-transform: uppercase; font-weight: 600; }
.map-value { color: var(--ink); font-size: 0.82rem; margin-top: 4px; word-break: break-all; }
.map-value.ok { color: var(--good); }
.map-value.miss { color: var(--low); }

.fault-banner {
    background: #fdecea;
    border: 1px solid #e8b4b4;
    border-radius: 12px;
    padding: 14px 16px;
    margin: 8px 0 18px;
    color: #5c1a1a;
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.65;
    font-size: 0.92rem;
}
.fault-banner .fault-title { font-weight: 600; margin-bottom: 6px; }

div[data-testid="stAlert"] {
    overflow-wrap: anywhere !important;
    word-break: break-word !important;
    white-space: pre-wrap !important;
}
</style>
"""

CSS_RTL = """
<style>
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] { direction: rtl; }
.fault-banner { direction: rtl; text-align: right; }
.section-title, .brand-title, .empty { direction: rtl; }
</style>
"""


def _lang() -> str:
    current = st.session_state.get("ui_lang")
    if current in {"en", "ar"}:
        return current
    lang = i18n.normalize(load_settings().get("ui_language"))
    st.session_state["ui_lang"] = lang
    return lang


def t(key: str, **kwargs: object) -> str:
    return i18n.translate(_lang(), key, **kwargs)


def _set_lang(lang: str) -> None:
    lang = i18n.normalize(lang)
    st.session_state["ui_lang"] = lang
    try:
        save_settings({"ui_language": lang})
    except OSError:
        pass
    st.rerun()


st.markdown(CSS, unsafe_allow_html=True)
if _lang() == "ar":
    st.markdown(CSS_RTL, unsafe_allow_html=True)


def _counts() -> dict[str, int]:
    c = db.count_by_status()
    for key in ("new", "qualified", "drafted", "approved", "sending", "sent", "replied", "closed", "rejected", "bounced"):
        c.setdefault(key, 0)
    return c


def _short_fault(text: str) -> str:
    en = llm.humanize_fault(text)
    mapped = {
        "Batch synthesis could not be completed.": t("fault_batch"),
        "Local inference endpoint is unavailable.": t("fault_local"),
        "Model quota is exhausted.": t("fault_quota"),
    }
    return mapped.get(en, en)


def _notify(message: str) -> None:
    st.session_state["_toast"] = message
    st.session_state.pop("_last_fault", None)
    st.session_state.pop("_last_fault_tb", None)


def _remember_fault(exc: BaseException) -> None:
    text = f"{type(exc).__name__}: {exc}"
    try:
        nice = llm.short_llm_error(exc)
        if nice and not llm.looks_like_model_dump(nice):
            text = nice
    except Exception:
        pass
    st.session_state["_last_fault"] = _short_fault(text) or "Operation could not be completed."
    raw_tb = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-2000:]}"
    st.session_state["_last_fault_tb"] = "" if llm.looks_like_model_dump(raw_tb) else raw_tb


def _flush_notifications() -> None:
    msg = st.session_state.pop("_toast", None)
    if msg:
        st.toast(str(msg))
    flash = st.session_state.pop("_settings_flash", None)
    if flash:
        st.toast(str(flash))
    err = st.session_state.pop("_settings_error", None)
    if err:
        st.session_state["_last_fault"] = str(err)
    fault = st.session_state.get("_last_fault")
    if not fault:
        return
    fault = str(fault).strip()
    st.session_state["_last_fault"] = fault
    st.markdown(
        f'<div class="fault-banner"><div class="fault-title">{_esc(t("fault_title"))}</div>{_esc(fault)}</div>',
        unsafe_allow_html=True,
    )
    tb = st.session_state.get("_last_fault_tb")
    if tb and tb.strip() and "NoneType: None" not in tb:
        with st.expander(t("diag"), expanded=False):
            st.code(tb[:3000])
    if st.button(t("fault_dismiss"), key="dismiss_fault"):
        st.session_state.pop("_last_fault", None)
        st.session_state.pop("_last_fault_tb", None)
        st.rerun()


def _esc(text: object) -> str:
    return html.escape(str(text or ""), quote=True)


def _score_kind(score: object) -> str:
    try:
        n = int(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "muted"
    if n >= 8:
        return "good"
    if n >= 6:
        return "mid"
    return "low"


_STATUS_KIND = {
    "new": "muted",
    "qualified": "mid",
    "drafted": "gold",
    "approved": "gold",
    "sending": "gold",
    "sent": "good",
    "replied": "good",
    "closed": "good",
    "rejected": "low",
    "bounced": "low",
    "scheduled": "muted",
    "processing": "gold",
    "failed": "low",
    "ready": "gold",
    "cancelled": "muted",
    "positive": "good",
    "negative": "low",
    "meeting_booked": "good",
    "closed_won": "good",
    "closed_lost": "low",
    "no_response": "muted",
}


def _badge(text: str, kind: str = "muted") -> str:
    return f'<span class="badge badge-{_esc(kind)}">{_esc(text)}</span>'


def _date_only(value: object) -> str:
    text = str(value or "").strip()
    return text[:10] if text else "—"


def _status_label(status: object) -> str:
    key = f"status_{status}"
    label = t(key)
    return str(status or "—") if label == key else label


def _status_badge(status: object) -> str:
    kind = _STATUS_KIND.get(str(status or ""), "muted")
    return _badge(_status_label(status), kind)


def _score_badge(score: object) -> str:
    try:
        n = int(score)  # type: ignore[arg-type]
        return _badge(t("icp_score", n=n), _score_kind(n))
    except (TypeError, ValueError):
        return _badge(t("icp_none"), "muted")


def _panel(text: str, arabic: bool = False) -> None:
    cls = "panel panel-ar" if arabic else "panel"
    body = _esc(text).replace("\n", "<br>")
    st.markdown(f'<div class="{cls}">{body or "—"}</div>', unsafe_allow_html=True)


def _linkedin_url_for(row: dict) -> str:
    url = db.extract_linkedin_url(row.get("linkedin_url"), row.get("source_notes"))
    if url:
        return url
    return db.linkedin_search_url(row.get("contact_name"), row.get("company_name")) or ""


def _linkedin_line(row: dict) -> None:
    url = _linkedin_url_for(row)
    if url:
        st.markdown(
            f'<div class="meta-link"><a href="{_esc(url)}" target="_blank" rel="noopener">{_esc(url)}</a></div>',
            unsafe_allow_html=True,
        )


def _lead_header(
    *,
    name: str,
    company: str,
    title: str = "",
    email: str = "",
    score: object = None,
    status: str = "",
    extra_badges: list[str] | None = None,
    extra_meta: str = "",
) -> None:
    bits = " · ".join(p for p in (title, email, extra_meta) if p)
    badges = []
    if score is not None and str(score) != "":
        badges.append(_score_badge(score))
    if status:
        badges.append(_status_badge(status))
    badges.extend(extra_badges or [])
    st.markdown(
        f"""
<div class="lead-card">
  <div class="lead-head">
    <div>
      <div class="lead-name">{_esc(name or "—")}</div>
      <div class="lead-co">{_esc(company or "—")}</div>
      <div class="lead-meta">{_esc(bits)}</div>
    </div>
    <div class="lead-badges">{"".join(badges)}</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


_NAV = (
    ("run", "nav_run"),
    ("review", "nav_review"),
    ("followups", "nav_followups"),
    ("ledger", "nav_ledger"),
    ("linkedin", "nav_linkedin"),
    ("settings", "nav_settings"),
)


def _nav() -> str:
    current = str(st.session_state.get("nav") or "run")
    if current in {"outbound", "rejected"}:
        return "ledger"
    return current


def _go(page: str) -> None:
    st.session_state["nav"] = page
    st.rerun()


def _engine_line() -> tuple[str, str, str]:
    settings = load_settings()
    provider = str(settings.get("llm_provider") or "auto")
    names = {
        "gemini": f"Gemini {settings.get('gemini_model') or '—'}",
        "grok": f"Grok {settings.get('grok_model') or '—'}",
        "qwen": f"Qwen {settings.get('lmstudio_model') or '—'}",
        "auto": t("engine_auto"),
    }
    active = names.get(provider, provider)
    failover = t("engine_qwen_local") if provider != "qwen" else t("engine_cloud")
    gate = t("engine_paused") if settings.get("pause_sending") else t("engine_live")
    return active, failover, gate


def render_nav() -> None:
    current = _nav()
    cols = st.columns(len(_NAV))
    for i, (key, label_key) in enumerate(_NAV):
        clicked = cols[i].button(
            t(label_key),
            key=f"nav_{key}",
            use_container_width=True,
            type="primary" if current == key else "secondary",
        )
        if clicked and current != key:
            if key == "ledger":
                st.session_state.setdefault("ledger_view", "outbox")
            _go(key)


def render_header() -> None:
    c = _counts()
    settings = load_settings()
    sent_today = db.get_today_send_count()
    cap = max(int(settings.get("daily_send_cap") or 0), 0)
    sent = c["sent"] + c["replied"] + c["closed"]
    total = sum(c.get(k, 0) for k in ("new", "qualified", "drafted", "approved", "sending", "sent", "replied", "closed", "rejected", "bounced"))
    active, failover, gate = _engine_line()
    frac = 0.0 if cap <= 0 else min(1.0, sent_today / cap)
    top, langbox = st.columns([5.6, 1.4])
    with top:
        st.markdown('<div class="brand-kicker">AI Outbound</div>', unsafe_allow_html=True)
        st.markdown(f'<h1 class="brand-title">{_esc(t("brand_title"))}</h1>', unsafe_allow_html=True)
    with langbox:
        a, b = st.columns(2)
        if a.button(t("lang_ar"), use_container_width=True, type="primary" if _lang() == "ar" else "secondary", key="lang_ar"):
            if _lang() != "ar":
                _set_lang("ar")
        if b.button(t("lang_en"), use_container_width=True, type="primary" if _lang() == "en" else "secondary", key="lang_en"):
            if _lang() != "en":
                _set_lang("en")
    st.markdown(
        f"""
<div class="engine-bar">
  <div class="engine-meta">
    <strong>{_esc(active)} [{_esc(t("engine_active"))}]</strong><br>
    {t("engine_failover")}: {_esc(failover)} · {t("engine_dispatch")} {_esc(gate)}
  </div>
  <div class="engine-meta" style="min-width:240px;flex:1">
    <strong>{_esc(t("quota_title"))}</strong> · {t("quota_today", sent=sent_today, cap=cap or "∞")}
    <div class="quota-track"><div class="quota-fill" style="width:{frac * 100:.1f}%"></div></div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )
    items = (
        ("run", t("kpi_total"), total, "outbox"),
        ("review", t("kpi_review"), c["drafted"], None),
        ("ledger", t("kpi_sent"), sent, "outbox"),
        ("ledger", t("kpi_replies"), c["replied"], "outbox"),
        ("ledger", t("kpi_rejected"), c["rejected"], "audit"),
    )
    cols = st.columns(5)
    for col, (page, label, value, ledger) in zip(cols, items):
        if col.button(f"{label}  ·  {value}", key=f"kpi_{page}_{label}", use_container_width=True):
            if ledger:
                st.session_state["ledger_view"] = ledger
            if _nav() != page:
                _go(page)


def _batch_reject_count(job: dict | None) -> int:
    if st.session_state.get("batch_open") is False:
        return 0
    if job:
        q = (job.get("summary") or {}).get("qualify") or {}
        n = int(q.get("below") or 0) + int(q.get("rule_rejected") or 0)
        if n:
            st.session_state["batch_rejects"] = n
            return n
    return int(st.session_state.get("batch_rejects") or 0)


def _render_job_panel(job: dict | None) -> None:
    if not job:
        st.markdown(f"**{t('job_ready')}**")
        return
    status = job.get("status")
    summary = job.get("summary") or {}
    q = summary.get("qualify") or {}
    d = summary.get("draft") or {}
    if status == "running":
        st.progress(min(max(float(job.get("progress") or 0.0), 0.02), 0.99), text=t("job_running"))
        return
    if status == "done":
        st.markdown(f"**{t('job_done')}**")
        rejected = int(q.get("below") or 0) + int(q.get("rule_rejected") or 0)
        st.markdown(t("job_qualified", n=int(q.get("above") or 0)))
        st.markdown(t("job_rejected", n=rejected))
        st.markdown(t("job_drafts", n=int(d.get("ok") or 0)))
        return
    st.error(t("job_fail"))


def _start_pipeline_job() -> None:
    st.session_state["batch_rejects"] = 0
    st.session_state["batch_open"] = True
    job_id = jobs.start_pipeline()
    if job_id is None:
        st.toast(t("job_busy"))
    st.rerun()


def _mapping_grid(mapped: dict) -> None:
    cells = []
    for key, label_key in (("Name", "map_name"), ("Email", "map_email"), ("Company", "map_company"), ("Title", "map_title")):
        value = str((mapped or {}).get(key) or "").strip()
        cls = "ok" if value else "miss"
        shown = value or t("map_missing")
        cells.append(
            f'<div class="map-cell"><div class="map-label">{_esc(t(label_key))}</div>'
            f'<div class="map-value {cls}">{_esc(shown)}</div></div>'
        )
    st.markdown(f'<div class="map-grid">{"".join(cells)}</div>', unsafe_allow_html=True)


def tab_run() -> None:
    c = _counts()
    running = jobs.is_running()
    job = db.get_latest_job(jobs.JOB_KIND)
    left, right = st.columns(2, gap="large")

    with left:
        with st.container(border=True):
            st.markdown(f'<div class="section-title">{_esc(t("ingest_title"))}</div>', unsafe_allow_html=True)
            uploaded = st.file_uploader(
                t("ingest_file"),
                type=["csv", "xlsx"],
                key="leads_upload",
                label_visibility="collapsed",
            )
            if uploaded:
                try:
                    inspect = csv_import.inspect_file(uploaded.getvalue(), uploaded.name)
                    st.session_state["last_inspect"] = inspect
                except Exception as exc:
                    _remember_fault(exc)
                    inspect = st.session_state.get("last_inspect") or {}
            else:
                inspect = st.session_state.get("last_inspect") or {}
            if inspect:
                st.caption(t("ingest_rows", n=int(inspect.get("rows") or 0)))
                _mapping_grid(inspect.get("mapped") or {})
            b1, b2 = st.columns(2)
            do_import = b1.button(t("ingest_only"), use_container_width=True, key="do_import", disabled=running or not uploaded)
            do_all = b2.button(t("ingest_all"), type="primary", use_container_width=True, key="do_all", disabled=running or not uploaded)

            if uploaded and (do_import or do_all):
                try:
                    ingest = csv_import.import_csv(uploaded.getvalue(), uploaded.name)
                    st.session_state["last_ingest"] = ingest
                    st.session_state["batch_rejects"] = 0
                    st.session_state["batch_open"] = False
                    if do_all:
                        _start_pipeline_job()
                    else:
                        st.rerun()
                except Exception as exc:
                    _remember_fault(exc)
                    st.rerun()

            ingest = st.session_state.get("last_ingest")
            if ingest:
                st.markdown(
                    f"**{t('ingest_added', added=int(ingest.get('inserted') or 0), dupes=int(ingest.get('skipped_dupes') or 0))}**"
                )

    with right:
        with st.container(border=True):
            st.markdown(f'<div class="section-title">{_esc(t("batch_title"))}</div>', unsafe_allow_html=True)
            a, b, ccol = st.columns(3)
            a.metric(t("batch_new"), c["new"])
            b.metric(t("batch_qualified"), c["qualified"])
            ccol.metric(t("batch_dq"), _batch_reject_count(job))
            if st.button(
                t("batch_run"),
                type="primary",
                use_container_width=True,
                key="run_local",
                disabled=running or not (c["new"] or c["qualified"]),
            ):
                _start_pipeline_job()

    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("orch_title"))}</div>', unsafe_allow_html=True)
        if running:
            _live_job_panel()
        else:
            _render_job_panel(job)


@st.fragment(run_every="2s")
def _live_job_panel() -> None:
    job = db.get_latest_job(jobs.JOB_KIND)
    _render_job_panel(job)
    if not job or job.get("status") != "running":
        st.rerun(scope="app")


_OUTCOME_KEYS = (
    "no_response",
    "replied",
    "positive",
    "negative",
    "meeting_booked",
    "closed_won",
    "closed_lost",
    "bounced",
)
_OUTCOME_REPLIED = {"replied", "positive", "negative", "meeting_booked", "closed_won", "closed_lost"}


def _outcome_label(key: str) -> str:
    return _status_label(key)


def _step_label(step: int) -> str:
    return t(f"step_{step}") if step in {1, 2, 3} else t("step_1")


def _kind_label(kind: str) -> str:
    return {
        "initial": t("kind_initial"),
        "followup_1": t("kind_fu1"),
        "followup_2": t("kind_fu2"),
        "followup_3": t("kind_fu3"),
    }.get(kind, kind)
_THREAD_KINDS_FOR_STEP = {
    1: ("initial",),
    2: ("initial", "followup_1"),
    3: ("initial", "followup_1", "followup_2"),
}
def _fu_status_label(status: object) -> str:
    return _status_label(status)


def _seq(rows: list[dict]) -> list[dict]:
    return [{"#": i, **row} for i, row in enumerate(rows, 1)]


def _reject_reasons() -> list[str]:
    return [t("reason_icp"), t("reason_nonsys"), t("reason_samefirm"), t("reason_role"), t("reason_draft"), t("reason_other")]


def _peer_status_label(status: object) -> str:
    return {
        "drafted": t("peer_draft"),
        "approved": t("peer_draft"),
        "sending": t("peer_sending"),
        "sent": t("peer_sent"),
        "replied": t("peer_replied"),
        "closed": t("peer_closed"),
    }.get(str(status or ""), str(status or ""))


def _reject_source_label(source: object) -> str:
    return {
        "rule": t("src_rule"),
        "llm": t("src_llm"),
        "manual": t("src_manual"),
        "unknown": t("src_unknown"),
    }.get(str(source or ""), str(source or ""))

_SENT_STATUSES = {"sent", "replied", "closed"}


def _company_peer_warning(peers: list[dict], code: str | None = None) -> None:
    if not peers:
        return
    sent = [p for p in peers if p.get("status") in _SENT_STATUSES]
    pending = [p for p in peers if p.get("status") not in _SENT_STATUSES]
    if sent and pending:
        headline = t("peer_inflight")
    elif sent:
        headline = t("peer_contacted")
    else:
        headline = t("peer_pending")
    if code:
        headline += f" · {code}"
    with st.expander(headline, expanded=False):
        for peer in sent + pending:
            name = peer.get("contact_name") or "—"
            email = peer.get("contact_email") or ""
            title = peer.get("title") or ""
            status = _peer_status_label(peer.get("status"))
            st.markdown(f"**{name}** · `{email}`" + (f" · {title}" if title else "") + f" — {status}")


def _li_kind_label(kind: object) -> str:
    return {"person": t("li_person"), "company": t("li_company"), "search": t("li_search")}.get(str(kind or ""), str(kind or ""))


def _card_frame_fields(row: dict, settings: dict, key: str) -> tuple[str, str, str]:
    greet_val = row["greeting"] if row.get("greeting") is not None else (settings.get("greeting") or "")
    sign_val = row["signoff"] if row.get("signoff") is not None else (settings.get("signoff") or "")
    sig_val = row["signature"] if row.get("signature") is not None else (settings.get("signature") or "")
    c1, c2 = st.columns(2)
    greeting = c1.text_input(t("greeting"), value=greet_val, key=f"{key}_greet")
    signoff = c2.text_input(t("signoff"), value=sign_val, key=f"{key}_signoff")
    signature = st.text_area(t("signature"), value=sig_val, key=f"{key}_sig", height=70)
    return greeting, signoff, signature


def tab_review() -> None:
    settings = load_settings()
    rows = db.get_review_queue()
    n_fu = db.count_followups_ready() if hasattr(db, "count_followups_ready") else 0
    if n_fu:
        st.caption(t("fu_ready_count", n=n_fu))
    if not rows:
        st.markdown(f'<div class="empty">{_esc(t("review_empty"))}</div>', unsafe_allow_html=True)
        return
    smtp = emailer.smtp_status()
    if not smtp["ready"]:
        st.warning(t("smtp_not_ready"))

    peers_by_lead = db.outreach_peers_map(rows)
    codes = db.company_code_map(rows) if hasattr(db, "company_code_map") else {}
    if rows and any(codes.get(int(r["id"])) for r in rows) and not st.session_state.get("peer_drafts_diversified"):
        st.session_state["peer_drafts_diversified"] = True
        try:
            n = sending.diversify_same_company_drafts(rows)
            if n:
                st.rerun()
        except Exception as exc:  # noqa: BLE001
            _remember_fault(exc)

    lo = int(settings["email_word_min"])
    hi = int(settings["email_word_max"])
    for i, row in enumerate(rows, 1):
        lid = row["id"]
        code = codes.get(int(lid))
        peers = peers_by_lead.get(int(lid)) or []
        extra = [_badge(str(i), "muted")]
        if code:
            extra.append(_badge(str(code), "gold"))
        if peers:
            extra.append(_badge(t("same_firm"), "mid"))
        with st.container(border=True):
            _lead_header(
                name=row.get("contact_name") or "—",
                company=row.get("company_name") or "—",
                title=row.get("title") or "",
                email=row.get("contact_email") or "",
                score=row.get("fit_score"),
                status="drafted",
                extra_badges=extra,
            )
            _linkedin_line(row)
            with st.expander(t("dossier"), expanded=(i == 1)):
                _company_peer_warning(peers, code)
                if row.get("fit_reasoning"):
                    st.markdown(f"**{t('synthesis')}**")
                    _panel(row["fit_reasoning"], arabic=_has_arabic_text(row["fit_reasoning"]))
                if row.get("source_notes"):
                    with st.expander(t("source_notes")):
                        _panel(row["source_notes"])
                hook = st.text_area(
                    t("hook"),
                    value=row.get("hook_used") or row.get("suggested_hook") or "",
                    key=f"hook_{lid}_{row['draft_id']}",
                    height=68,
                )
                subject = st.text_input(t("subject"), value=row.get("subject") or "", key=f"subj_{lid}_{row['draft_id']}")
                body = st.text_area(t("body"), value=row.get("body") or "", key=f"body_{lid}_{row['draft_id']}", height=160)
                greeting, signoff, signature = _card_frame_fields(row, settings, f"frame_{lid}_{row['draft_id']}")
                words = len((body or "").split())
                band = "good" if lo <= words <= hi else "mid"
                st.markdown(_badge(t("words", n=words, lo=lo, hi=hi), band), unsafe_allow_html=True)
                preview = sending.preview_email(
                    row.get("contact_name"),
                    subject,
                    body,
                    greeting=greeting,
                    signoff=signoff,
                    signature=signature,
                )
                st.markdown(f"**{t('preview')}**")
                _panel(preview["body"])
                if row.get("body_ar") or row.get("subject_ar"):
                    st.markdown(f"**{t('translation')}**")
                    if row.get("subject_ar"):
                        st.caption(row["subject_ar"])
                    if row.get("body_ar"):
                        _panel(row["body_ar"], arabic=True)
                r1, r2 = st.columns(2)
                r1.selectbox(t("dq_reason"), _reject_reasons(), key=f"rej_why_{lid}")
                r2.text_input(t("dq_detail"), key=f"rej_note_{lid}", placeholder=t("optional"))
                b1, b2, b3 = st.columns([1.2, 1, 1])
                lock_key = f"approve_lock_{lid}"
                locked = bool(st.session_state.get(lock_key))
                with b1:
                    if st.button(t("approve_send"), type="primary", use_container_width=True, key=f"approve_{lid}", disabled=locked):
                        st.session_state[lock_key] = True
                        try:
                            with st.spinner(t("dispatching")):
                                sending.approve_and_send(
                                    row,
                                    subject,
                                    body,
                                    hook,
                                    greeting=greeting,
                                    signoff=signoff,
                                    signature=signature,
                                )
                            _notify(t("notify_sent"))
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.session_state[lock_key] = False
                            _remember_fault(exc)
                            st.rerun()
                with b2:
                    if st.button(t("regenerate"), use_container_width=True, key=f"regen_{lid}", disabled=locked):
                        try:
                            with st.spinner(t("synthesizing")):
                                draft = sending.draft_with_company_diversity(row, hook=hook)
                                sending.persist_draft(lid, draft["subject"], draft["body"], hook, kind="initial")
                            _notify(t("notify_rewritten"))
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            _remember_fault(exc)
                            st.rerun()
                with b3:
                    if st.button(t("disqualify"), use_container_width=True, key=f"reject_{lid}", disabled=locked):
                        why = st.session_state.get(f"rej_why_{lid}") or _reject_reasons()[0]
                        note = (st.session_state.get(f"rej_note_{lid}") or "").strip()
                        reason = f"{why} — {note}" if note else str(why)
                        db.reject_lead(lid, reason)
                        _notify(t("notify_dq"))
                        st.rerun()


def tab_followups() -> None:
    settings = load_settings()
    smtp = emailer.smtp_status()
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("cadence_title"))}</div>', unsafe_allow_html=True)
        bits = []
        bits.append(_badge(t("enabled"), "good") if settings.get("followup_enabled") else _badge(t("halted"), "low"))
        if db.is_weekend():
            bits.append(_badge(t("weekend"), "muted"))
        bits.append(_badge(t("smtp_ready"), "good") if smtp.get("ready") else _badge(t("smtp_down"), "low"))
        bits.append(_badge(t("operator_replies"), "gold"))
        st.markdown(" ".join(bits), unsafe_allow_html=True)
        st.caption(t("radar_note"))
        if st.button(t("prepare_due"), type="primary", key="run_due", use_container_width=True):
            try:
                with st.spinner(t("preparing")):
                    stats = sending.prepare_due_followups(limit=10, pace=False)
                _notify(t("notify_prepared", ok=stats.get("drafted", 0), failed=stats["failed"]))
            except Exception as exc:  # noqa: BLE001
                _remember_fault(exc)
            st.rerun()

    due_n = len(db.get_due_followups())
    ready = db.get_followup_review_queue()
    if ready:
        st.markdown(f'<div class="section-title">{_esc(t("ready_dispatch", n=len(ready)))}</div>', unsafe_allow_html=True)
        if not smtp["ready"]:
            st.warning(t("smtp_not_ready"))
        fu_codes = db.company_code_map(ready)
        if st.button(t("return_ready"), use_container_width=True, key="restore_all_fu"):
            n = db.restore_ready_followups()
            st.session_state["fu_auto_prepared"] = True
            _notify(t("notify_returned_n", n=n))
            st.rerun()
        for i, row in enumerate(ready, 1):
            _render_followup_card(row, settings, fu_codes.get(int(row["id"])), index=i)
    elif due_n:
        st.markdown(f"**{t('due_today', n=due_n)}**")

    rows = db.get_followup_queue()
    pending = _cadence_ledger_rows(rows)
    if not pending and not ready:
        st.markdown(f'<div class="empty">{_esc(t("cadence_empty"))}</div>', unsafe_allow_html=True)
        return
    if pending:
        st.markdown(f'<div class="section-title">{_esc(t("cadence_ledger", n=len(pending)))}</div>', unsafe_allow_html=True)
        h1, h2, h3, h4, h5, h6, h7, h8, h9 = st.columns([0.4, 1.4, 1.5, 1.0, 1.15, 1.15, 1.15, 1.4, 0.75])
        h1.caption("#")
        h2.caption(t("col_name"))
        h3.caption(t("col_company"))
        h4.caption(t("col_step"))
        h5.caption(t("col_opening"))
        h6.caption(t("col_fu1_sent"))
        h7.caption(t("col_due"))
        h8.caption(t("col_outcome"))
        h9.caption(t("col_control"))
        for i, row in enumerate(pending, 1):
            _render_followup_row(i, row)


def _cadence_ledger_rows(rows: list[dict]) -> list[dict]:
    """One open row per person. Sent follow-ups stay in the database, not this table."""
    open_status = {"scheduled", "processing", "failed"}
    open_rows = [r for r in rows if r.get("status") in open_status]
    by_lead: dict[int, dict] = {}
    for row in open_rows:
        lid = int(row["lead_id"])
        prev = by_lead.get(lid)
        if prev is None or int(row.get("step") or 99) < int(prev.get("step") or 99):
            by_lead[lid] = row
    keep = {int(r["id"]) for r in by_lead.values()}
    return [r for r in open_rows if int(r["id"]) in keep]


def _watch_send(row: dict) -> tuple[int | None, str]:
    step = int(row.get("step") or 1)
    status = str(row.get("status") or "")
    if status == "sent":
        if step >= 2 and row.get("fu2_send_id"):
            return int(row["fu2_send_id"]), str(row.get("fu2_outcome") or "no_response")
        if row.get("fu1_send_id"):
            return int(row["fu1_send_id"]), str(row.get("fu1_outcome") or "no_response")
    if step >= 2 and row.get("fu1_send_id"):
        return int(row["fu1_send_id"]), str(row.get("fu1_outcome") or "no_response")
    sid = row.get("initial_send_id")
    return (int(sid) if sid else None), str(row.get("initial_outcome") or "no_response")


def _render_followup_row(index: int, row: dict) -> None:
    fid = int(row["id"])
    lid = int(row.get("lead_id") or 0)
    step = int(row.get("step") or 1)
    status = row.get("status")
    send_id, current = _watch_send(row)
    keys = list(_OUTCOME_KEYS)
    try:
        idx = keys.index(current if current in keys else "no_response")
    except ValueError:
        idx = 0
    c1, c2, c3, c4, c5, c6, c7, c8, c9 = st.columns([0.4, 1.4, 1.5, 1.0, 1.15, 1.15, 1.15, 1.4, 0.75])
    c1.markdown(f"**{index}**")
    c2.markdown(row.get("contact_name") or "—")
    c3.markdown(row.get("company_name") or "—")
    c4.markdown(_step_label(step))
    c4.caption(_fu_status_label(status))
    c5.markdown(_date_only(row.get("initial_sent_at")))
    c6.markdown(_date_only(row.get("fu1_sent_at")))
    c7.markdown(_date_only(row.get("scheduled_at")))
    with c8:
        choice = st.selectbox(
            t("col_outcome"),
            options=keys,
            index=idx,
            format_func=_outcome_label,
            key=f"fu_out_{fid}",
            label_visibility="collapsed",
        )
    with c9:
        if status == "failed":
            if st.button(t("retry"), key=f"retry_fu_{fid}", use_container_width=True):
                db.retry_followup(fid)
                st.session_state["fu_auto_prepared"] = False
                st.rerun()
        elif status in {"scheduled", "processing"}:
            if st.button(t("cancel"), key=f"cancel_fu_{fid}", use_container_width=True):
                db.mark_followup(fid, "cancelled", error="Cancelled from cadence ledger")
                st.rerun()
        else:
            c9.caption("—")
    if choice != current and send_id:
        _record_outcome(lid, choice, choice in _OUTCOME_REPLIED, send_id)


def _render_thread_messages(messages: list[dict], contact_name: str | None, kind_key: str = "kind") -> None:
    for msg in messages:
        kind = str(msg.get(kind_key) or msg.get("kind") or msg.get("send_kind") or "initial")
        label = _kind_label(kind)
        composed = sending.preview_email(
            contact_name,
            msg.get("subject") or "",
            msg.get("body") or "",
            draft=msg,
        )
        st.markdown(f"**{label}** · {_date_only(msg.get('sent_at'))}")
        if composed.get("subject"):
            st.caption(composed["subject"])
        _panel(composed.get("body") or msg.get("body") or "")
        if msg.get("body_ar") or msg.get("subject_ar"):
            st.markdown(f"**{t('translation')}**")
            if msg.get("subject_ar"):
                st.caption(msg["subject_ar"])
            if msg.get("body_ar"):
                _panel(msg["body_ar"], arabic=True)


def _render_sent_thread(lead_id: int, contact_name: str | None, step: int) -> None:
    wanted = set(_THREAD_KINDS_FOR_STEP.get(int(step) or 1, ("initial",)))
    thread = [m for m in db.get_sent_thread(lead_id) if (m.get("kind") or "initial") in wanted]
    if not thread:
        return
    with st.expander(t("thread_history"), expanded=False):
        _render_thread_messages(thread, contact_name)


def _render_followup_card(row: dict, settings: dict, company_code: str | None = None, index: int | None = None) -> None:
    fid = int(row["followup_id"])
    lid = int(row["id"])
    step = int(row["step"])
    original = db.initial_send_for_lead(lid)
    extra = []
    if index:
        extra.append(_badge(str(index), "muted"))
    extra.append(_badge(t("fu_badge", n=step), "gold"))
    if company_code:
        extra.append(_badge(str(company_code), "gold"))
    with st.container(border=True):
        _lead_header(
            name=row.get("contact_name") or "—",
            company=row.get("company_name") or "—",
            title=row.get("title") or "",
            email=row.get("contact_email") or "",
            status="ready",
            extra_badges=extra,
            extra_meta=f"{_step_label(step)} · {_date_only(row.get('scheduled_at'))}",
        )
        _linkedin_line(row)
        with st.expander(t("draft_dispatch"), expanded=False):
            _render_sent_thread(lid, row.get("contact_name"), step)
            hook = st.text_area(
                t("hook"),
                value=row.get("hook_used") or row.get("suggested_hook") or "",
                key=f"fu_hook_{fid}_{row['draft_id']}",
                height=68,
            )
            subject = st.text_input(t("subject"), value=row.get("subject") or "", key=f"fu_subj_{fid}_{row['draft_id']}")
            body = st.text_area(t("body"), value=row.get("body") or "", key=f"fu_body_{fid}_{row['draft_id']}", height=160)
            greeting, signoff, signature = _card_frame_fields(row, settings, f"fu_frame_{fid}_{row['draft_id']}")
            words = len((body or "").split())
            st.caption(t("words", n=words, lo=settings["email_word_min"], hi=settings["email_word_max"]))
            preview = sending.preview_email(
                row.get("contact_name"),
                subject,
                body,
                greeting=greeting,
                signoff=signoff,
                signature=signature,
                kind=f"followup_{step}",
            )
            st.markdown(f"**{t('preview')}**")
            _panel(preview["body"])
            if row.get("body_ar") or row.get("subject_ar"):
                st.markdown(f"**{t('translation')}**")
                if row.get("subject_ar"):
                    st.caption(row["subject_ar"])
                if row.get("body_ar"):
                    _panel(row["body_ar"], arabic=True)
            lock_key = f"fu_approve_lock_{fid}"
            locked = bool(st.session_state.get(lock_key))
            if step == 2:
                links = sending.drive_links_block(settings)
                if links:
                    st.caption(t("drive_will_append"))
                    _panel(links)
                else:
                    st.caption(t("drive_missing"))
            b1, b2, b3, b4 = st.columns(4)
            with b1:
                if st.button(t("approve_send"), type="primary", use_container_width=True, key=f"fu_approve_{fid}", disabled=locked):
                    st.session_state[lock_key] = True
                    try:
                        with st.spinner(t("dispatching")):
                            sending.approve_and_send_followup(
                                row,
                                subject,
                                body,
                                hook,
                                greeting=greeting,
                                signoff=signoff,
                                signature=signature,
                            )
                        _notify(t("notify_fu_sent"))
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        st.session_state[lock_key] = False
                        _remember_fault(exc)
                        st.rerun()
            with b2:
                if st.button(t("regenerate"), use_container_width=True, key=f"fu_regen_{fid}", disabled=locked):
                    try:
                        orig = original or {}
                        with st.spinner(t("synthesizing")):
                            draft = sending.draft_with_company_diversity(
                                row,
                                hook=hook,
                                kind=f"followup_{step}",
                                original_subject=orig.get("subject") or "",
                                original_body=orig.get("body") or "",
                                step=step,
                            )
                        subj = sending.thread_subject(orig.get("subject"), draft["subject"])
                        draft_id = sending.persist_draft(lid, subj, draft["body"], hook, kind=f"followup_{step}")
                        db.mark_followup(fid, "ready", draft_id=draft_id)
                        _notify(t("notify_rewritten"))
                        st.rerun()
                    except Exception as exc:  # noqa: BLE001
                        _remember_fault(exc)
                        st.rerun()
            with b3:
                if st.button(t("return_schedule"), use_container_width=True, key=f"fu_unready_{fid}", disabled=locked):
                    if db.restore_followup_to_schedule(fid):
                        st.session_state["fu_auto_prepared"] = True
                        _notify(t("notify_returned"))
                    st.rerun()
            with b4:
                if st.button(t("skip"), use_container_width=True, key=f"fu_skip_{fid}", disabled=locked):
                    db.mark_followup(fid, "cancelled", error="Skipped from review")
                    _notify(t("notify_skip"))
                    st.rerun()


def _record_outcome(lead_id: int, outcome: str, replied: bool, send_id: int | None = None) -> None:
    db.mark_outcome(lead_id, outcome, replied, send_id=send_id)
    _notify(t("notify_outcome"))
    st.rerun()


def tab_ledger() -> None:
    view = str(st.session_state.get("ledger_view") or "outbox")
    a, b = st.columns(2)
    if a.button(t("ledger_outbox"), use_container_width=True, type="primary" if view == "outbox" else "secondary", key="led_out"):
        st.session_state["ledger_view"] = "outbox"
        st.rerun()
    if b.button(t("ledger_audit"), use_container_width=True, type="primary" if view == "audit" else "secondary", key="led_aud"):
        st.session_state["ledger_view"] = "audit"
        st.rerun()
    if st.session_state.get("ledger_view") == "audit":
        tab_rejected()
    else:
        tab_tracker()


def tab_tracker() -> None:
    rows = db.get_tracker_rows()
    if not rows:
        st.markdown(f'<div class="empty">{_esc(t("outbox_empty"))}</div>', unsafe_allow_html=True)
        return
    codes = db.company_code_map() if hasattr(db, "company_code_map") else {}
    threads = _group_outbound_threads(rows)
    bounced, replied, active = [], [], []
    for thread in threads:
        bucket = _thread_bucket(thread)
        if bucket == "bounced":
            bounced.append(thread)
        elif bucket == "replied":
            replied.append(thread)
        else:
            active.append(thread)
    if replied:
        st.markdown(f'<div class="section-title">{_esc(t("responses", n=len(replied)))}</div>', unsafe_allow_html=True)
        for i, thread in enumerate(replied, 1):
            _render_outbound_thread(i, thread, codes)
    if bounced:
        st.markdown(f'<div class="section-title">{_esc(t("bounces", n=len(bounced)))}</div>', unsafe_allow_html=True)
        for i, thread in enumerate(bounced, 1):
            _render_outbound_thread(i, thread, codes)
    if active:
        st.markdown(f'<div class="section-title">{_esc(t("outbox", n=len(active)))}</div>', unsafe_allow_html=True)
        for i, thread in enumerate(active, 1):
            _render_outbound_thread(i, thread, codes)
    elif not replied and not bounced:
        st.markdown(f'<div class="empty">{_esc(t("outbox_empty"))}</div>', unsafe_allow_html=True)


def _group_outbound_threads(rows: list[dict]) -> list[list[dict]]:
    groups: dict[int, list[dict]] = {}
    order: list[int] = []
    for row in rows:
        lid = int(row["lead_id"])
        if lid not in groups:
            groups[lid] = []
            order.append(lid)
        groups[lid].append(row)
    return [list(reversed(groups[lid])) for lid in order]


def _thread_bucket(thread: list[dict]) -> str:
    lead_status = str((thread[-1] if thread else {}).get("lead_status") or "")
    outcomes = {str(m.get("outcome") or "") for m in thread}
    if lead_status == "bounced" or "bounced" in outcomes:
        return "bounced"
    if lead_status in {"replied", "closed"} or bool(outcomes & _OUTCOME_REPLIED):
        return "replied"
    return "active"


def _render_outbound_thread(index: int, thread: list[dict], codes: dict) -> None:
    if not thread:
        return
    latest = thread[-1]
    latest_kind = str(latest.get("send_kind") or "initial")
    extra = [
        _badge(str(index), "muted"),
        _badge(_kind_label(latest_kind), "gold"),
        _badge(t("messages_n", n=len(thread)), "muted"),
        _badge(_outcome_label(str(latest.get("outcome") or "no_response")), "muted"),
    ]
    code = codes.get(int(latest["lead_id"]))
    if code:
        extra.append(_badge(str(code), "gold"))
    with st.container(border=True):
        _lead_header(
            name=latest.get("contact_name") or "—",
            company=latest.get("company_name") or "—",
            title=latest.get("title") or "",
            email=latest.get("contact_email") or "",
            score=latest.get("fit_score"),
            status=latest.get("lead_status") or "sent",
            extra_badges=extra,
            extra_meta=_date_only(latest.get("sent_at")),
        )
        _linkedin_line(latest)
        with st.expander(t("client_file"), expanded=False):
            _render_thread_outcome(thread)
        if latest.get("fit_reasoning"):
            with st.expander(t("icp_synthesis"), expanded=False):
                _panel(str(latest["fit_reasoning"]), arabic=_has_arabic_text(latest["fit_reasoning"]))
        with st.expander(t("thread"), expanded=False):
            _render_thread_messages(thread, latest.get("contact_name"), kind_key="send_kind")


def _render_thread_outcome(thread: list[dict]) -> None:
    latest = thread[-1]
    lid = int(latest["lead_id"])
    send_id = int(latest["send_id"])
    current = next((str(m["outcome"]) for m in reversed(thread) if m.get("outcome")), "no_response")
    keys = list(_OUTCOME_KEYS)
    saved = str(latest.get("outcome_note") or "")
    with st.form(key=f"out_form_{lid}", border=False):
        choice = st.selectbox(
            t("col_outcome"),
            options=keys,
            index=keys.index(current) if current in keys else 0,
            format_func=_outcome_label,
            key=f"out_thread_{lid}",
        )
        note = st.text_area(
            t("outcome_note"),
            value=saved,
            placeholder=t("outcome_note_ph"),
            key=f"out_note_{lid}",
            height=90,
        )
        submitted = st.form_submit_button(t("save_note"), type="primary")
    if saved and latest.get("outcome_note_at"):
        st.caption(t("note_updated", when=_date_only(latest["outcome_note_at"])))
    if not submitted:
        return
    if choice != current:
        db.mark_outcome(lid, choice, choice in _OUTCOME_REPLIED, send_id=send_id)
    if note.strip() != saved.strip():
        db.set_outcome_note(lid, note)
    _notify(t("notify_note_saved"))
    st.rerun()


def _has_arabic_text(text: object) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", str(text or "")))


def tab_rejected() -> None:
    rows = db.get_rejected_leads()
    if not rows:
        st.markdown(f'<div class="empty">{_esc(t("audit_empty"))}</div>', unsafe_allow_html=True)
        return
    q = st.text_input(
        "Search",
        placeholder=t("search_audit"),
        key="rej_search",
        label_visibility="collapsed",
    )
    needle = (q or "").strip().lower()
    filtered = rows
    if needle:
        filtered = [
            r
            for r in rows
            if needle
            in " ".join(
                str(r.get(k) or "")
                for k in ("company_name", "contact_name", "contact_email", "title", "reject_reason", "reject_detail", "fit_reasoning")
            ).lower()
        ]
    st.caption(f"{len(filtered)} / {len(rows)}")
    ids = [int(r["id"]) for r in filtered]
    labels = {int(r["id"]): f"{r.get('contact_name') or '—'} · {r.get('company_name') or '—'}" for r in filtered}
    pick = st.selectbox(
        t("restore_gateway"),
        options=[0] + ids,
        format_func=lambda x: t("select_prospect") if not x else labels.get(int(x), str(x)),
        key="rej_restore_pick",
    )
    if st.button(t("restore_gateway"), use_container_width=True, disabled=not pick, key="rej_restore_go"):
        status = db.restore_rejected_to_draft(int(pick))
        _notify(t("notify_restored") if status == "drafted" else t("notify_restored_q"))
        st.rerun()
    st.dataframe(
        _seq(
            [
                {
                    t("col_company"): r.get("company_name") or "—",
                    t("col_name"): r.get("contact_name") or "—",
                    t("col_email"): r.get("contact_email") or "",
                    t("col_title"): r.get("title") or "",
                    t("col_score"): r.get("fit_score") if r.get("fit_score") is not None else "",
                    t("col_source"): _reject_source_label(r.get("reject_source")),
                    t("col_rationale"): r.get("reject_reason") or r.get("fit_reasoning") or "—",
                    "LinkedIn": r.get("linkedin_url") or "",
                    t("col_when"): str(r.get("rejected_at") or r.get("created_at") or "")[:10],
                }
                for r in filtered
            ]
        ),
        hide_index=True,
        use_container_width=True,
        column_config={"LinkedIn": st.column_config.LinkColumn("LinkedIn")},
    )


def tab_linkedin() -> None:
    rows = db.get_linkedin_directory()
    if not rows:
        st.markdown(f'<div class="empty">{_esc(t("net_empty"))}</div>', unsafe_allow_html=True)
        return
    q = st.text_input(
        "filter",
        placeholder=t("search_net"),
        key="li_search",
        label_visibility="collapsed",
    )
    needle = (q or "").strip().lower()
    filtered = rows
    if needle:
        filtered = [
            r
            for r in rows
            if needle
            in " ".join(
                str(r.get(k) or "")
                for k in ("company_name", "contact_name", "contact_email", "title")
            ).lower()
        ]
    n_person = sum(1 for r in filtered if r.get("linkedin_kind") == "person")
    n_company = sum(1 for r in filtered if r.get("linkedin_kind") == "company")
    n_reached = sum(1 for r in filtered if r.get("linkedin_reached"))
    codes = db.company_code_map(filtered) if hasattr(db, "company_code_map") else {}
    m1, m2, m3, m4 = st.columns(4)
    m1.metric(t("net_prospects"), len(filtered))
    m2.metric(t("net_profiles"), n_person)
    m3.metric(t("net_pages"), n_company)
    m4.metric(t("net_reached"), n_reached)
    table = _seq(
        [
            {
                "id": int(r["id"]),
                "Reached": bool(r.get("linkedin_reached")),
                "Code": codes.get(int(r["id"])) or "",
                "Company": r.get("company_name") or "—",
                "Name": r.get("contact_name") or "—",
                "Title": r.get("title") or "",
                "Email": r.get("contact_email") or "",
                "Score": r.get("fit_score") if r.get("fit_score") is not None else "",
                "Status": _status_label(r.get("status")),
                "Link type": _li_kind_label(r.get("linkedin_kind")),
                "LinkedIn": r.get("linkedin_url") or "",
            }
            for r in filtered
        ]
    )
    edited = st.data_editor(
        table,
        hide_index=True,
        use_container_width=True,
        column_order=["#", "Reached", "Code", "Company", "Name", "Title", "Email", "Score", "Status", "Link type", "LinkedIn"],
        disabled=["#", "Code", "Company", "Name", "Title", "Email", "Score", "Status", "Link type", "LinkedIn", "id"],
        column_config={
            "id": None,
            "Reached": st.column_config.CheckboxColumn(t("net_reached")),
            "LinkedIn": st.column_config.LinkColumn("LinkedIn"),
        },
        key="li_reached_editor",
    )
    edited_rows = edited.to_dict("records") if hasattr(edited, "to_dict") else list(edited or [])
    by_id = {int(r["id"]): bool(r.get("linkedin_reached")) for r in filtered}
    for row in edited_rows:
        lid = int(row.get("id") or 0)
        if not lid:
            continue
        want = bool(row.get("Reached"))
        if want != by_id.get(lid):
            db.set_linkedin_reached(lid, want)
    links = "\n".join(
        f"{r.get('contact_name') or ''} | {r.get('company_name') or ''} | {r.get('linkedin_url') or ''}"
        for r in filtered
        if r.get("linkedin_url")
    )
    st.download_button(
        t("export_csv"),
        data=(
            "reached,company,name,title,email,score,status,link_type,linkedin\n"
            + "\n".join(
                ",".join(
                    [
                        "1" if r.get("Reached") else "0",
                        '"' + str(r.get("Company") or "").replace('"', '""') + '"',
                        '"' + str(r.get("Name") or "").replace('"', '""') + '"',
                        '"' + str(r.get("Title") or "").replace('"', '""') + '"',
                        str(r.get("Email") or ""),
                        str(r.get("Score") or ""),
                        str(r.get("Status") or ""),
                        str(r.get("Link type") or ""),
                        str(r.get("LinkedIn") or ""),
                    ]
                )
                for r in (edited_rows or table)
            )
        ),
        file_name="ai-outbound-linkedin.csv",
        mime="text/csv",
        use_container_width=True,
    )
    with st.expander(t("copy_links")):
        st.code(links or "—", language=None)


def _commit_settings(updates: dict, ok_message: str) -> None:
    previous = load_settings()
    try:
        verified = save_settings(updates)
    except OSError as exc:
        st.session_state["_settings_error"] = t("save_failed", exc=exc)
        st.rerun()
        return
    bad: list[str] = []
    for key, want in updates.items():
        got = verified.get(key)
        if isinstance(want, bool):
            if bool(got) != bool(want):
                bad.append(f"{key}={got}")
        elif isinstance(want, int):
            try:
                if int(got) != int(want):
                    bad.append(f"{key}={got}")
            except (TypeError, ValueError):
                bad.append(key)
        elif got != want:
            bad.append(key)
    if bad:
        st.session_state["_settings_error"] = t("save_not_persist", keys=", ".join(bad))
        st.rerun()
        return
    extra = sending.apply_settings_to_inflight(previous)
    flash = ok_message.rstrip(".")
    if extra:
        flash = f"{flash} · {extra}"
    st.session_state["_settings_flash"] = flash + "."
    st.rerun()


def tab_settings() -> None:
    settings = load_settings()
    if settings.get("_load_error"):
        st.error(t("settings_bad", path=settings.get("_load_path") or ""))

    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("llm_routing"))}</div>', unsafe_allow_html=True)
        provider = st.radio(
            t("active_engine"),
            options=["auto", "gemini", "grok", "qwen"],
            index=["auto", "gemini", "grok", "qwen"].index(settings["llm_provider"])
            if settings["llm_provider"] in {"auto", "gemini", "grok", "qwen"}
            else 1,
            format_func=lambda x: {
                "auto": t("prov_auto"),
                "gemini": t("prov_gemini"),
                "grok": t("prov_grok"),
                "qwen": t("prov_qwen"),
            }[x],
            horizontal=True,
        )
        if st.button(t("save_engine"), use_container_width=True, key="save_provider"):
            _commit_settings({"llm_provider": provider}, t("engine_saved"))
        if st.button(t("test_engine"), use_container_width=True, key="test_llm"):
            try:
                with st.spinner(t("testing_engine")):
                    result = llm.test_llm()
                st.success(result)
                _notify(result)
            except Exception as exc:  # noqa: BLE001
                _remember_fault(exc)
                st.rerun()

    st.markdown("")
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("key_vault"))}</div>', unsafe_allow_html=True)
        with st.form("add_llm_key"):
            k1, k2, k3 = st.columns([1.2, 2.2, 1.4])
            k_prov = k1.selectbox(t("provider"), options=["gemini", "grok"], format_func=lambda x: "Gemini" if x == "gemini" else "Grok")
            k_secret = k2.text_input(t("api_key"), type="password", value="")
            k_label = k3.text_input(t("label"), value="", placeholder=t("optional"))
            if st.form_submit_button(t("add_key"), use_container_width=True):
                try:
                    llm_keys.add_key(str(k_prov), k_secret, k_label)
                    st.session_state["_settings_flash"] = t("key_added")
                except Exception as exc:  # noqa: BLE001
                    st.session_state["_settings_error"] = str(exc)
                st.rerun()
        key_rows = llm_keys.list_keys()
        if key_rows:
            for item in key_rows:
                until = ""
                if item.get("blocked") and item.get("blocked_until"):
                    until = datetime.fromtimestamp(float(item["blocked_until"])).strftime("%m-%d %H:%M")
                cols = st.columns([1, 1.2, 1.6, 2, 0.8])
                cols[0].markdown(str(item.get("provider") or ""))
                cols[1].markdown(str(item.get("label") or ""))
                cols[2].markdown(str(item.get("masked") or ""))
                cols[3].markdown(t("blocked_until", when=until) if item.get("blocked") else t("ready"))
                if cols[4].button(t("remove"), key=f"del_key_{item['id']}"):
                    llm_keys.remove_key(str(item["id"]))
                    st.rerun()
        else:
            st.caption(t("no_keys"))

    st.markdown("")
    with st.container(border=True):
        st.markdown('<div class="section-title">Gemini</div>', unsafe_allow_html=True)
        gem_options = list(llm.GEMINI_CHAT_MODELS)
        gem_current = llm.normalize_gemini_model(settings.get("gemini_model") or "")
        if gem_current not in gem_options:
            gem_options = [gem_current] + gem_options
        with st.form("gemini_settings"):
            gem_model = st.selectbox(
                t("preferred_model"),
                options=gem_options,
                index=gem_options.index(gem_current) if gem_current in gem_options else 0,
            )
            rotate = st.checkbox(t("rotate_models"), value=bool(settings.get("gemini_rotate_models", True)))
            g1, g2 = st.columns(2)
            rpm = g1.number_input(t("rpm"), min_value=1, max_value=60, value=int(settings.get("gemini_rpm") or 8))
            day_cap = g2.number_input(t("day_cap"), min_value=0, max_value=100000, value=int(settings.get("gemini_daily_cap_per_model") or 0))
            if st.form_submit_button(t("save_gemini"), use_container_width=True):
                _commit_settings(
                    {
                        "gemini_model": llm.normalize_gemini_model(gem_model) or llm.GEMINI_CHAT_MODELS[0],
                        "gemini_rotate_models": bool(rotate),
                        "gemini_rpm": int(rpm),
                        "gemini_daily_cap_per_model": int(day_cap),
                    },
                    t("gemini_saved"),
                )
        usage = llm.gemini_usage_by_key()
        if usage:
            st.markdown(f"**{t('usage_today')}**")
            for item in usage:
                status = t("blocked") if item.get("blocked") else t("ready")
                st.markdown(f"{item.get('label') or 'Gemini'} · {item.get('masked') or ''} · {int(item.get('total') or 0)} · {status}")
                models = item.get("models") or []
                if models:
                    for m in models:
                        st.caption(f"{m.get('model')} · {int(m.get('used') or 0)} · {m.get('status')}")
                else:
                    st.caption(t("no_usage"))
            if st.button(t("clear_blocks"), key="reset_blocks"):
                llm.reset_gemini_blocks()
                st.rerun()

    st.markdown("")
    with st.container(border=True):
        st.markdown('<div class="section-title">Grok</div>', unsafe_allow_html=True)
        grok_options = list(llm.GROK_CHAT_MODELS)
        grok_current = str(settings.get("grok_model") or grok_options[0])
        if grok_current not in grok_options:
            grok_options = [grok_current] + grok_options
        with st.form("grok_settings"):
            grok_model = st.selectbox(
                t("preferred_model"),
                options=grok_options,
                index=grok_options.index(grok_current) if grok_current in grok_options else 0,
            )
            if st.form_submit_button(t("save_grok"), use_container_width=True):
                _commit_settings({"grok_model": str(grok_model).strip() or llm.GROK_CHAT_MODELS[0]}, t("grok_saved"))

    st.markdown("")
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("qwen_title"))}</div>', unsafe_allow_html=True)
        with st.form("qwen_settings"):
            q1, q2 = st.columns(2)
            lm_url = q1.text_input(t("endpoint"), value=settings["lmstudio_base_url"])
            lm_model = q2.text_input(t("model"), value=settings["lmstudio_model"])
            if st.form_submit_button(t("save_qwen"), use_container_width=True):
                _commit_settings(
                    {
                        "lmstudio_base_url": lm_url.strip() or "http://127.0.0.1:1234/v1",
                        "lmstudio_model": lm_model.strip(),
                    },
                    t("qwen_saved"),
                )
        if st.button(t("list_lms"), use_container_width=True, key="list_lms"):
            try:
                st.write(llm.list_qwen_models(settings["lmstudio_base_url"] or None))
            except Exception as exc:  # noqa: BLE001
                _remember_fault(exc)
                st.rerun()

    st.markdown("")
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("throughput"))}</div>', unsafe_allow_html=True)
        with st.form("batch_settings"):
            b1, b2 = st.columns(2)
            qb = b1.number_input(t("qualify_batch"), min_value=1, max_value=25, value=int(settings.get("qualify_batch_size") or 10))
            dbatch = b2.number_input(t("draft_batch"), min_value=1, max_value=6, value=int(settings.get("draft_batch_size") or 3))
            rules_on = st.checkbox(t("prefilter"), value=bool(settings.get("rule_prefilter", True)))
            if st.form_submit_button(t("save_throughput"), use_container_width=True):
                _commit_settings(
                    {
                        "qualify_batch_size": int(qb),
                        "draft_batch_size": int(dbatch),
                        "rule_prefilter": bool(rules_on),
                    },
                    t("throughput_saved"),
                )

    st.markdown("")
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("safety"))}</div>', unsafe_allow_html=True)
        with st.form("safety_settings"):
            s1, s2, s3 = st.columns(3)
            cap = s1.number_input(t("daily_quota"), min_value=0, max_value=500, value=int(settings.get("daily_send_cap") or 0))
            dmin = s2.number_input(t("min_delay"), min_value=0, max_value=600, value=int(settings.get("delay_min_seconds") or 60))
            dmax = s3.number_input(t("max_delay"), min_value=0, max_value=1200, value=int(settings.get("delay_max_seconds") or 240))
            w1, w2 = st.columns(2)
            wh_start = w1.number_input(t("window_start"), min_value=0, max_value=23, value=int(settings.get("send_window_start_hour") or 9))
            wh_end = w2.number_input(t("window_end"), min_value=1, max_value=24, value=int(settings.get("send_window_end_hour") or 17))
            pause = st.checkbox(t("pause_all"), value=bool(settings["pause_sending"]))
            if st.form_submit_button(t("save_safety"), use_container_width=True):
                _commit_settings(
                    {
                        "daily_send_cap": int(cap),
                        "delay_min_seconds": int(dmin),
                        "delay_max_seconds": int(max(dmax, dmin)),
                        "send_window_start_hour": int(wh_start),
                        "send_window_end_hour": int(wh_end),
                        "pause_sending": bool(pause),
                    },
                    t("safety_saved"),
                )

    st.markdown("")
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("copy_cadence"))}</div>', unsafe_allow_html=True)
        with st.form("copy_settings"):
            n1, n2 = st.columns(2)
            sender_name = n1.text_input(t("sender_name"), value=settings["sender_name"])
            sender_title = n2.text_input(t("sender_title"), value=settings["sender_title"])
            offer = st.text_area(t("offer"), value=settings["offer"], height=70)
            g1, g2 = st.columns(2)
            greeting = g1.text_input(
                t("greeting"),
                value=settings.get("greeting") if settings.get("greeting") is not None else "Hi {first_name},",
            )
            signoff = g2.text_input(t("signoff"), value=settings.get("signoff") or "")
            signature = st.text_area(t("signature"), value=settings["signature"], height=80)
            _panel(
                sending.compose_with(
                    greeting,
                    signoff,
                    signature,
                    "This is where the generated paragraphs go. They end with a short question?",
                    "Jane Doe",
                )
            )
            w1, w2, min_score = st.columns(3)
            word_min = w1.number_input(t("min_words"), min_value=20, max_value=250, value=int(settings["email_word_min"]))
            word_max = w2.number_input(t("max_words"), min_value=20, max_value=300, value=int(settings["email_word_max"]))
            min_score = min_score.number_input(t("icp_threshold"), min_value=1, max_value=10, value=int(settings["qualify_min_score"]))
            followup_enabled = st.checkbox(t("schedule_fu"), value=bool(settings["followup_enabled"]))
            f1, f2, f3 = st.columns(3)
            fu1 = f1.number_input(t("fu1_days"), min_value=1, max_value=30, value=int(settings["followup_1_days"]))
            fu2 = f2.number_input(t("fu2_days"), min_value=1, max_value=60, value=int(settings["followup_2_days"]))
            max_fu = f3.number_input(t("fu_depth"), min_value=0, max_value=2, value=min(2, int(settings["max_followups"])))
            with st.expander(t("prompts_icp")):
                icp = st.text_area(t("icp_def"), value=settings["icp"], height=150)
                qualify_prompt = st.text_area(t("prompt_score"), value=settings["qualify_prompt"], height=180)
                draft_prompt = st.text_area(t("prompt_open"), value=settings["draft_prompt"], height=180)
                followup_prompt = st.text_area(t("prompt_fu1"), value=settings["followup_prompt"], height=160)
                followup_2_prompt = st.text_area(
                    t("prompt_fu2"),
                    value=settings.get("followup_2_prompt") or DEFAULT_FOLLOWUP_2_PROMPT,
                    height=200,
                )
            saved = st.form_submit_button(t("save_copy"), type="primary", use_container_width=True)
        if saved:
            _commit_settings(
                {
                    "icp": icp,
                    "qualify_min_score": int(min_score),
                    "qualify_prompt": qualify_prompt,
                    "sender_name": sender_name,
                    "sender_title": sender_title,
                    "offer": offer,
                    "greeting": greeting.strip(),
                    "signoff": signoff.strip(),
                    "signature": signature,
                    "email_word_min": int(word_min),
                    "email_word_max": int(max(word_max, word_min)),
                    "draft_prompt": draft_prompt,
                    "followup_prompt": followup_prompt,
                    "followup_2_prompt": followup_2_prompt,
                    "followup_enabled": bool(followup_enabled),
                    "followup_1_days": int(fu1),
                    "followup_2_days": int(fu2),
                    "followup_3_days": int(settings.get("followup_3_days") or 2),
                    "max_followups": min(2, int(max_fu)),
                },
                t("copy_saved"),
            )
        if st.button(t("reset_prompts"), use_container_width=True, key="reset_prompts"):
            _commit_settings(
                {
                    "icp": DEFAULT_ICP,
                    "qualify_prompt": DEFAULT_QUALIFY_PROMPT,
                    "draft_prompt": DEFAULT_DRAFT_PROMPT,
                    "followup_prompt": DEFAULT_FOLLOWUP_PROMPT,
                    "followup_2_prompt": DEFAULT_FOLLOWUP_2_PROMPT,
                    "followup_3_prompt": DEFAULT_FOLLOWUP_3_PROMPT,
                },
                t("prompts_reset"),
            )

    st.markdown("")
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("attachments"))}</div>', unsafe_allow_html=True)
        st.caption(t("drive_hint"))
        with st.form("drive_links"):
            drive_folder = st.text_input(
                t("drive_folder"),
                value=settings.get("drive_folder_url") or settings.get("drive_case_study_url") or "",
            )
            saved_drive = st.form_submit_button(t("save_drive"), type="primary", use_container_width=True)
        if saved_drive:
            _commit_settings(
                {
                    "drive_folder_url": drive_folder.strip(),
                    "max_followups": 2,
                },
                t("drive_saved"),
            )

    st.markdown("")
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("smtp_title"))}</div>', unsafe_allow_html=True)
        with st.form("smtp_settings"):
            s1, s2 = st.columns(2)
            host = s1.text_input("SMTP_HOST", value=env_get("SMTP_HOST", "smtp.zoho.com"))
            port = s2.text_input("SMTP_PORT", value=env_get("SMTP_PORT", "465"))
            s3, s4 = st.columns(2)
            user = s3.text_input("SMTP_USER", value=env_get("SMTP_USER", ""))
            from_addr = s4.text_input("SMTP_FROM", value=env_get("SMTP_FROM", "") or user)
            use_ssl = st.selectbox(
                "SMTP_USE_SSL",
                options=["true", "false"],
                index=0 if env_get("SMTP_USE_SSL", "true").lower() != "false" else 1,
            )
            password = st.text_input("SMTP_PASSWORD", type="password", value="", placeholder=t("smtp_blank"))
            st.caption(t("smtp_configured") if emailer.smtp_status()["password_configured"] else t("smtp_missing"))
            saved_smtp = st.form_submit_button(t("save_smtp"), use_container_width=True)
        if saved_smtp:
            updates = {
                "SMTP_HOST": host.strip(),
                "SMTP_PORT": port.strip() or "465",
                "SMTP_USER": user.strip(),
                "SMTP_FROM": from_addr.strip(),
                "SMTP_USE_SSL": use_ssl,
            }
            if password.strip():
                updates["SMTP_PASSWORD"] = password.strip()
            upsert_env(updates)
            st.session_state["_settings_flash"] = t("smtp_saved")
            st.rerun()
        if st.button(t("test_smtp"), use_container_width=True, key="test_smtp"):
            try:
                with st.spinner(t("testing_smtp")):
                    result = emailer.test_connection()
                _notify(result)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                _remember_fault(exc)
                st.rerun()

    st.markdown("")
    with st.container(border=True):
        st.markdown(f'<div class="section-title">{_esc(t("persist"))}</div>', unsafe_allow_html=True)
        loc = db.db_location()
        sloc = settings_location()
        st.markdown(f"**{t('sqlite')}**")
        st.code(loc["path"], language=None)
        if loc["in_onedrive"]:
            st.warning(t("db_onedrive"))
        else:
            st.caption(t("off_onedrive") + (" · " + t("legacy_copy") if loc["legacy_exists"] else ""))
        st.markdown(f"**{t('settings_file')}**")
        st.code(sloc["path"], language=None)
        if sloc["in_onedrive"]:
            st.warning(t("settings_onedrive"))
        elif sloc.get("legacy_exists"):
            st.caption(t("legacy_copy"))
        if st.button(t("backup_now"), use_container_width=True, key="backup_db"):
            try:
                src = Path(loc["path"])
                dest = src.with_name(f"ai-outbound-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db")
                shutil.copy2(src, dest)
                _notify(t("backup_ok", name=dest.name))
            except Exception as exc:  # noqa: BLE001
                _remember_fault(exc)
            st.rerun()


def main() -> None:
    st.session_state.setdefault("nav", "run")
    st.session_state.setdefault("ledger_view", "outbox")
    render_header()
    render_nav()
    _flush_notifications()
    page = _nav()
    if page == "review":
        tab_review()
    elif page == "followups":
        tab_followups()
    elif page == "ledger":
        tab_ledger()
    elif page == "linkedin":
        tab_linkedin()
    elif page == "settings":
        tab_settings()
    else:
        tab_run()


main()
