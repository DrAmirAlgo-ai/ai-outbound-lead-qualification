"""Local settings (non-secrets) and .env helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
LEGACY_SETTINGS_PATH = ROOT / "settings.json"

DEFAULT_ICP = """Ideal customer for Your Company:
- Hedge funds, proprietary trading desks, CTAs, and family offices.
- The contact plausibly runs or oversees systematic / quantitative trading, execution, or trading-risk architecture.
- They could commission bespoke work: algorithmic execution engines, risk-governance systems, or multi-account trade copiers (cTrader / MT5).
Reject: retail signal sellers, indicator vendors, generic marketing roles, banks with no systematic book, and anyone with no evidence of systematic trading."""

DEFAULT_QUALIFY_PROMPT = """You are helping a solo quantitative-trading-systems architect evaluate whether a lead is worth a personalized cold email.

Ideal customer profile:
{icp}

Lead:
Company: {company_name}
Contact: {contact_name}
Title: {title}
Notes: {notes}

1. Score fit from 1-10. Be conservative — most leads should score below {min_score} unless there is clear evidence of systematic trading activity.
2. Give 1-2 sentences of reasoning.
3. If score >= {min_score}, suggest one specific, honest personalization hook based only on the notes. Do not invent facts."""

DEFAULT_DRAFT_PROMPT = """Write a cold email from {sender_name}, {sender_title}, to {contact_name} at {company_name}.

Offer / positioning: {offer}
Context/hook: {hook}

Requirements:
- Between {word_min} and {word_max} words in the body.
- Start directly with the first sentence. NO greeting line (no Hi/Hello/Dear + name).
- NO sign-off and NO signature (no Best regards, Best, Thanks, your name, title, or company block). The system adds the greeting and signature itself.
- Calm, peer-to-peer institutional tone — not salesy, not desperate.
- Reference the hook naturally in one sentence. Do not invent facts.
- Offer to share a technical case study and backtest audit as a first step — do not attach anything, just offer.
- End with a simple, low-pressure question.
- No hype words, no exclamation marks, no superlatives."""

DEFAULT_FOLLOWUP_PROMPT = """Write follow-up email number {step} from {sender_name}, {sender_title}, to {contact_name} at {company_name}.

The first email was sent and there has been no reply. Do not guilt them. Do not claim they promised anything.

Original subject: {original_subject}
Original body: {original_body}
Hook: {hook}

Requirements:
- Between {word_min} and {word_max} words.
- Start directly with the first sentence. NO greeting line (no Hi/Hello/Dear + name).
- NO sign-off and NO signature (no Best regards, Best, Thanks, your name, title, or company block). The system adds them.
- Same calm institutional tone.
- Add one new useful angle (a case study, a risk/execution detail, or a short question) without repeating the first email word for word.
- No hype, no exclamation marks."""

DEFAULT_FOLLOWUP_2_PROMPT = """Write follow-up 2 from {sender_name}, {sender_title}, to {contact_name} at {company_name}.

The opening email and follow-up 1 already went out. There has been no reply. Do not guilt them. Do not claim they promised anything.

Original subject: {original_subject}
Original body: {original_body}
Hook: {hook}

This letter must read differently from follow-up 1. Do not reuse that five-paragraph structure. Do not list numbered facts. Do not quote trade counts, profit factor, return, drawdown, percentages, or any other figure.

Angle for this letter:
- One Google Drive folder link will be appended by the system after you write. Mention that a single folder is at the end of this note. Do not invent a URL and do not paste a placeholder.
- The folder holds four things, and the letter should name them in plain language: a written case study of how the architecture was scoped and built; tick-data backtest results; a short note showing that execution matches across accounts over the same period; and a video of the multi-account copy engine running live, with positions staying identical across linked accounts.
- If this URL is present, refer to it only as "the folder below":
Folder: {drive_folder_url}

Requirements:
- Between {word_min} and {word_max} words.
- Start directly with the first sentence. NO greeting line (no Hi/Hello/Dear + name).
- NO sign-off and NO signature. The system adds them.
- Two or three short paragraphs, then one low-pressure call to action: invite them to a short call. Do not ask them to review the files instead. Do not propose a specific time.
- Calm institutional tone. No hype, no exclamation marks, no superlatives.
- Do not repeat follow-up 1 or the opening email word for word."""

DEFAULT_FOLLOWUP_3_PROMPT = """Write a short follow-up email number {step} from {sender_name}, {sender_title}, to {contact_name} at {company_name}.

A video is attached by the system. Mention that a short video is attached. Do not invent what is in the video.

Original subject: {original_subject}
Hook: {hook}

Requirements:
- Between {word_min} and {word_max} words. Keep it short.
- Start directly with the first sentence. NO greeting line, NO sign-off, NO signature.
- Do not repeat backtest numbers or the previous follow-up.
- End with one short question.
- Calm institutional tone. No hype, no exclamation marks."""

DEFAULT_SIGNATURE = """Your Name
Your Title
Your Company"""

DEFAULT_OFFER = """Your Company designs algorithmic execution engines, institutional risk-governance systems, and multi-account trade copiers for cTrader/MT5."""

DEFAULT_TITLES = """Head of Quantitative Research
Head of Quant
CIO
Chief Investment Officer
Head of Trading
Head of Execution
Portfolio Manager
Quantitative Researcher
Managing Partner
Founder
Head of Risk
CTO"""

DEFAULT_SENIORITIES = """founder
c_suite
partner
vp
head
director"""

DEFAULT_LOCATIONS = """United States
United Kingdom
United Arab Emirates
Switzerland
Singapore
Hong Kong
European Union"""

DEFAULT_ORG_KEYWORDS = """hedge fund
proprietary trading
family office
quantitative
systematic
CTA
multi-strategy"""

DEFAULT_EMPLOYEE_RANGES = """11,50
51,200
201,500
501,1000
1001,5000
5001,10000"""

DEFAULT_SETTINGS = {
    "llm_provider": "qwen",
    "lmstudio_base_url": "http://127.0.0.1:1234/v1",
    "lmstudio_model": "qwen3.5-9b",
    "gemini_model": "gemini-3.5-flash-lite",
    "grok_model": "grok-4-fast-non-reasoning",
    "gemini_rotate_models": True,
    "gemini_rpm": 8,
    "gemini_daily_cap_per_model": 20,
    "qualify_batch_size": 10,
    "draft_batch_size": 3,
    "rule_prefilter": True,
    "daily_send_cap": 15,
    "delay_min_seconds": 60,
    "delay_max_seconds": 240,
    "qualify_min_score": 6,
    "email_word_min": 55,
    "email_word_max": 80,
    "sender_name": "Your Name",
    "sender_title": "Your Title",
    "offer": DEFAULT_OFFER,
    "signature": DEFAULT_SIGNATURE,
    "signoff": "Best regards",
    "greeting": "Hi {first_name},",
    "followup_jitter_min_minutes": 45,
    "followup_jitter_max_minutes": 240,
    "send_window_start_hour": 9,
    "send_window_end_hour": 17,
    "reply_tracking_enabled": False,
    "inbox_lookback_days": 14,
    "icp": DEFAULT_ICP,
    "qualify_prompt": DEFAULT_QUALIFY_PROMPT,
    "draft_prompt": DEFAULT_DRAFT_PROMPT,
    "followup_prompt": DEFAULT_FOLLOWUP_PROMPT,
    "followup_2_prompt": DEFAULT_FOLLOWUP_2_PROMPT,
    "followup_3_prompt": DEFAULT_FOLLOWUP_3_PROMPT,
    "include_winning_examples": False,
    "auto_send_on_approve": True,
    "followup_enabled": True,
    "followup_1_days": 3,
    "followup_2_days": 4,
    "followup_3_days": 2,
    "max_followups": 2,
    "drive_folder_url": "",
    "pause_sending": False,
    "ui_language": "ar",
    "apollo_max_per_run": 20,
    "apollo_include_similar_titles": True,
    "apollo_titles": DEFAULT_TITLES,
    "apollo_seniorities": DEFAULT_SENIORITIES,
    "apollo_locations": DEFAULT_LOCATIONS,
    "apollo_org_keywords": DEFAULT_ORG_KEYWORDS,
    "apollo_employee_ranges": DEFAULT_EMPLOYEE_RANGES,
    "apollo_keywords": "systematic quantitative trading",
}


_JSON_RETURN_LINE = re.compile(r"\n*Return strict JSON[^\n]*", re.I)


def strip_json_instructions(text: str) -> str:
    """Drop machine JSON contracts so the user can edit writing instructions only."""
    raw = (text or "").strip()
    raw = _JSON_RETURN_LINE.sub("", raw)
    raw = re.sub(r"\n*\{[^{}\n]{0,200}\}\s*$", "", raw)
    return raw.strip()


def persist_dir() -> Path:
    """Same folder as the SQLite DB: off OneDrive when the project lives there."""
    load_dotenv(ENV_PATH, override=False)
    override = os.environ.get("AIOUTBOUND_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser().parent
    if "onedrive" in str(ROOT).lower():
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / "AIOutbound"
    return ROOT


def _migrate_legacy_file(legacy: Path, dest: Path) -> None:
    if dest == legacy or dest.exists() or not legacy.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(legacy, dest)
    except OSError:
        pass


def settings_path() -> Path:
    dest = persist_dir() / "settings.json"
    _migrate_legacy_file(LEGACY_SETTINGS_PATH, dest)
    return dest


def settings_location() -> dict:
    path = settings_path()
    return {
        "path": str(path),
        "in_onedrive": "onedrive" in str(path).lower(),
        "legacy_path": str(LEGACY_SETTINGS_PATH),
        "legacy_exists": LEGACY_SETTINGS_PATH.exists() and LEGACY_SETTINGS_PATH.resolve() != path.resolve(),
    }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_json_dict(path: Path) -> dict | None:
    """Return the object if it is a JSON dict. None = missing. Raises if corrupt."""
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise json.JSONDecodeError("empty", raw, 0)
    saved = json.loads(raw)
    if not isinstance(saved, dict):
        raise json.JSONDecodeError("not an object", raw, 0)
    return saved


def load_env() -> None:
    load_dotenv(ENV_PATH, override=True)


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    path = settings_path()
    try:
        saved = _read_json_dict(path)
        if saved:
            data.update(saved)
    except (json.JSONDecodeError, OSError):
        # Keep defaults in memory; never treat a corrupt file as empty on save.
        data["_load_error"] = True
        data["_load_path"] = str(path)
    int_keys = [
        "daily_send_cap",
        "delay_min_seconds",
        "delay_max_seconds",
        "qualify_min_score",
        "email_word_min",
        "email_word_max",
        "followup_1_days",
        "followup_2_days",
        "followup_3_days",
        "max_followups",
        "apollo_max_per_run",
        "gemini_rpm",
        "gemini_daily_cap_per_model",
        "qualify_batch_size",
        "draft_batch_size",
        "followup_jitter_min_minutes",
        "followup_jitter_max_minutes",
        "send_window_start_hour",
        "send_window_end_hour",
        "inbox_lookback_days",
    ]
    for key in int_keys:
        try:
            data[key] = int(data.get(key, DEFAULT_SETTINGS[key]))
        except (TypeError, ValueError):
            data[key] = DEFAULT_SETTINGS[key]
    for key in (
        "include_winning_examples",
        "auto_send_on_approve",
        "followup_enabled",
        "pause_sending",
        "apollo_include_similar_titles",
        "gemini_rotate_models",
        "rule_prefilter",
        "reply_tracking_enabled",
    ):
        data[key] = bool(data.get(key, DEFAULT_SETTINGS[key]))
    if data["followup_jitter_min_minutes"] < 0:
        data["followup_jitter_min_minutes"] = 0
    if data["followup_jitter_max_minutes"] < data["followup_jitter_min_minutes"]:
        data["followup_jitter_max_minutes"] = data["followup_jitter_min_minutes"]
    data["send_window_start_hour"] = max(0, min(23, data["send_window_start_hour"]))
    if data["send_window_end_hour"] <= data["send_window_start_hour"]:
        data["send_window_end_hour"] = min(24, data["send_window_start_hour"] + 1)
    if "greeting" not in data or data.get("greeting") is None:
        data["greeting"] = DEFAULT_SETTINGS["greeting"]
    if data["delay_min_seconds"] < 1:
        data["delay_min_seconds"] = 1
    if data["delay_max_seconds"] < data["delay_min_seconds"]:
        data["delay_max_seconds"] = data["delay_min_seconds"]
    if data["email_word_max"] < data["email_word_min"]:
        data["email_word_max"] = data["email_word_min"]
    provider = str(data.get("llm_provider") or "gemini").lower()
    if provider not in {"gemini", "grok", "qwen", "auto"}:
        provider = "gemini"
    data["llm_provider"] = provider
    lang = str(data.get("ui_language") or "ar").strip().lower()
    data["ui_language"] = "en" if lang == "en" else "ar"
    if not str(data.get("drive_folder_url") or "").strip():
        data["drive_folder_url"] = str(data.get("drive_case_study_url") or "").strip()
    data["reply_tracking_enabled"] = False
    data["include_winning_examples"] = False
    for key in ("qualify_prompt", "draft_prompt", "followup_prompt", "followup_2_prompt", "followup_3_prompt"):
        data[key] = strip_json_instructions(str(data.get(key) or ""))
    return data


_OLD_DRAFT_RULE = "- Do not include any sign-off or signature (no Best regards, Best, Thanks, your name, or company block). Those are appended later."
_OLD_FOLLOWUP_RULE = "- Do not include any sign-off or signature (no Best regards, Best, Thanks, your name, or company block)."
_NEW_DRAFT_RULE = (
    "- Start directly with the first sentence. NO greeting line (no Hi/Hello/Dear + name).\n"
    "- NO sign-off and NO signature (no Best regards, Best, Thanks, your name, title, or company block). "
    "The system adds the greeting and signature itself."
)
_NEW_FOLLOWUP_RULE = (
    "- Start directly with the first sentence. NO greeting line (no Hi/Hello/Dear + name).\n"
    "- NO sign-off and NO signature (no Best regards, Best, Thanks, your name, title, or company block). The system adds them."
)


_INTERNAL_KEYS = ("_load_error", "_load_path")


def migrate_prompts() -> list[str]:
    """Bring saved prompts up to the no-greeting / no-sign-off contract.

    Saved prompts override the defaults, so without this the LLM would still be
    asked (by the old text) to write only 'no sign-off' and could keep adding 'Hi X,'.
    User customisations are kept: only the closing rule line is swapped, or the
    greeting rule is appended if the prompt has neither.
    """
    path = settings_path()
    try:
        saved = _read_json_dict(path)
    except (json.JSONDecodeError, OSError):
        return []
    if not saved:
        return []
    changed: list[str] = []
    for key, old, new in (
        ("draft_prompt", _OLD_DRAFT_RULE, _NEW_DRAFT_RULE),
        ("followup_prompt", _OLD_FOLLOWUP_RULE, _NEW_FOLLOWUP_RULE),
    ):
        cur = saved.get(key)
        if not isinstance(cur, str) or not cur.strip():
            continue
        if "NO greeting" in cur:
            continue
        if old in cur:
            saved[key] = cur.replace(old, new)
        elif "Requirements:" in cur:
            saved[key] = cur.replace("Requirements:", "Requirements:\n" + new, 1)
        else:
            saved[key] = new + "\n\n" + cur
        changed.append(key)
    if changed:
        _atomic_write_text(path, json.dumps(saved, indent=2, ensure_ascii=False))
    return changed


def save_settings(settings: dict) -> dict:
    """Merge, write atomically, and return the settings actually on disk."""
    path = settings_path()
    existing: dict = {}
    try:
        saved = _read_json_dict(path)
        if saved:
            existing = saved
    except (json.JSONDecodeError, OSError):
        if path.exists():
            bak = path.with_name(path.name + ".corrupt")
            try:
                os.replace(path, bak)
            except OSError:
                pass
    merged = dict(DEFAULT_SETTINGS)
    merged.update(existing)
    merged.update(settings)
    for key in ("qualify_prompt", "draft_prompt", "followup_prompt", "followup_2_prompt", "followup_3_prompt"):
        merged[key] = strip_json_instructions(str(merged.get(key) or ""))
    for key in _INTERNAL_KEYS:
        merged.pop(key, None)
    _atomic_write_text(path, json.dumps(merged, indent=2, ensure_ascii=False))
    verified = load_settings()
    for key in _INTERNAL_KEYS:
        verified.pop(key, None)
    return verified


def env_get(key: str, default: str = "") -> str:
    load_env()
    return os.getenv(key, default).strip()


def env_configured(key: str) -> bool:
    value = env_get(key)
    if not value:
        return False
    placeholders = {
        "sk-ant-your-key-here",
        "your-app-password",
        "you@yourdomain.com",
        "your-apollo-key",
        "your-gemini-key",
    }
    return value not in placeholders


def upsert_env(updates: dict[str, str]) -> None:
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    done: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}")
                done.add(key)
                continue
        new_lines.append(line)

    for key, value in updates.items():
        if key not in done:
            new_lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    load_env()


def lines_to_list(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
