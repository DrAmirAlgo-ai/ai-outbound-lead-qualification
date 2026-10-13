"""LLM router: local Qwen via LM Studio, or Gemini via Google AI Studio.

Gemini path is built for the free tier reality:
- Google's free tier allows ~20 requests/day *per model*. We rotate across
  several Flash models, remember which ones are exhausted/dead, and never
  hammer a model that already said no.
- A process-wide rate limiter spaces requests so we do not trip per-minute
  limits.
- Qualification is done in batches (many leads per request) so 40 leads cost
  4-5 requests instead of 40.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from settings_store import env_get, load_settings, strip_json_instructions

import llm_keys

ROOT = Path(__file__).resolve().parent
LLM_STATE_PATH = ROOT / "llm_state.json"


class LLMError(RuntimeError):
    pass


JSON_CONTRACT_QUALIFY = (
    "Return strict JSON only, no markdown fences: "
    '{"score": <int 1-10>, "reasoning": <string>, "hook": <string or null>}'
)
JSON_CONTRACT_EMAIL = (
    "Return strict JSON only, no markdown fences: "
    '{"subject": <string>, "body": <string>}'
)
JSON_CONTRACT_QUALIFY_BATCH = (
    "Return strict JSON only, no markdown fences, exactly one entry per lead id: "
    '{"results": [{"id": <int>, "score": <int 1-10>, "reasoning": <string>, "hook": <string or null>}]}'
)
JSON_CONTRACT_EMAIL_BATCH = (
    "Return strict JSON only, no markdown fences, one entry per id: "
    '{"emails": [{"id": <int>, "subject": <string>, "body": <string>}]}'
)
JSON_CONTRACT_AR = (
    "Return strict JSON only, no markdown fences: "
    '{"subject": <string>, "body": <string>}'
)
JSON_CONTRACT_TRANSLATE = (
    "Return strict JSON only, no markdown fences: "
    '{"items": [{"id": <int>, "ar": <string>}]}'
)

# Output budget for email JSON. High enough that a longer settings prompt
# (facts + structure + a few sample emails) does not get cut mid-object.
DRAFT_MAX_TOKENS = 8192
AR_MAX_TOKENS = 2500


def with_json_contract(user_prompt: str, contract: str) -> str:
    return strip_json_instructions(user_prompt).rstrip() + "\n\n" + contract


class QuotaExhausted(LLMError):
    """Every usable Gemini model is blocked (daily quota, dead, or cooling down)."""

    def __init__(self, message: str, retry_at: float | None = None):
        super().__init__(message)
        self.retry_at = retry_at


# --------------------------------------------------------------------------- #
# Generic helpers
# --------------------------------------------------------------------------- #


def fill_prompt(template: str, **values: object) -> str:
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def _strip_think(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I)
    cleaned = re.sub(r"<thinking>.*?</thinking>", "", cleaned, flags=re.S | re.I)
    return cleaned.strip()


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def looks_like_model_dump(text: str) -> bool:
    """True when the UI is about to show truncated Gemini JSON instead of a real error."""
    compact = " ".join((text or "").split())
    if not compact:
        return False
    if compact[0] in "{[":
        return True
    head = compact[:240].lower()
    return '"emails"' in head or ('"subject"' in head and '"body"' in head)


def humanize_fault(text: str) -> str:
    """One institutional line. Never dump model JSON into the dashboard."""
    compact = " ".join((text or "").split())
    if not compact:
        return ""
    for sep in (" — ", " —", " - ", "\n"):
        idx = compact.find(sep)
        if idx > 8:
            head, tail = compact[:idx].strip(), compact[idx + len(sep) :].strip()
            if head and looks_like_model_dump(tail):
                return "Batch synthesis could not be completed."
    if looks_like_model_dump(compact):
        return "Batch synthesis could not be completed."
    low = compact.lower()
    if any(m in low for m in ("قطع الرد", "max_tokens", "finishreason", "الدرافت الجماعي", "قطع رد الكتابة")):
        return "Batch synthesis could not be completed."
    if "127.0.0.1" in compact or "lm studio" in low or "qwen" in low:
        return "Local inference endpoint is unavailable."
    if "quota" in low or "حصة" in compact or "الحد اليومي" in compact:
        return "Model quota is exhausted."
    if len(compact) > 280:
        compact = compact[:280] + "…"
    return compact


def extract_json(text: str) -> dict[str, Any]:
    data = extract_json_any(text)
    if isinstance(data, dict):
        return data
    raise LLMError("JSON مش كائن.")


def _salvage_objects(raw: str) -> list[dict[str, Any]]:
    """Pull complete JSON objects out of a truncated Gemini reply."""
    dec = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    idx = 0
    while idx < len(raw):
        start = raw.find("{", idx)
        if start == -1:
            break
        try:
            obj, end = dec.raw_decode(raw, start)
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(obj, dict) and any(k in obj for k in ("id", "subject", "score", "emails", "results")):
            if "emails" in obj and isinstance(obj.get("emails"), list):
                found.extend(x for x in obj["emails"] if isinstance(x, dict))
            elif "results" in obj and isinstance(obj.get("results"), list):
                found.extend(x for x in obj["results"] if isinstance(x, dict))
            elif "id" in obj:
                found.append(obj)
        idx = end
    return found


def extract_json_any(text: str) -> Any:
    """Accept a JSON object or array. Salvage complete items if Gemini cut the reply short."""
    raw = _strip_fences(_strip_think(text or ""))
    first_obj = raw.find("{")
    first_arr = raw.find("[")
    candidates = [i for i in (first_obj, first_arr) if i != -1]
    if not candidates:
        raise LLMError("العقل مرجعش JSON.")
    start = min(candidates)
    closer = "}" if raw[start] == "{" else "]"
    end = raw.rfind(closer)
    blob = raw[start : end + 1] if end > start else raw[start:]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        salvaged = _salvage_objects(raw)
        if salvaged:
            return {"emails": salvaged, "results": salvaged}
        raise LLMError(
            "Gemini قطع الرد قبل ما يخلّص JSON. السيستم هيكمل بكتابة كل إيميل لوحده."
        ) from None


def short_llm_error(exc: BaseException) -> str:
    """One readable Arabic line — never dump a truncated JSON blob into the UI."""
    return humanize_fault(str(exc)) or type(exc).__name__


_CLOSE_LINE = re.compile(
    r"^(?:"
    r"best(?:\s+regards|\s+wishes)?"
    r"|kind\s+regards"
    r"|warm\s+regards"
    r"|regards"
    r"|sincerely(?:\s+yours)?"
    r"|yours(?:\s+sincerely|\s+truly)?"
    r"|thanks(?:\s+again)?"
    r"|thank\s+you"
    r"|cheers"
    r")"
    r"(?:\s*[,–—-]\s*[A-Za-z][A-Za-z.\- ]{0,40})?"
    r"(?:\s+[A-Za-z][A-Za-z.\- ]{0,40})?"
    r"[\s,.]*$",
    re.I,
)


_SIGNATURE_TITLE_LINE = re.compile(
    r"^(?:"
    r"(?:quantitative|systematic|algorithmic|trading|senior|principal|lead|head|chief|founder|managing)\b[^\n]{0,60}"
    r"|[A-Za-z][A-Za-z.\- ]{0,30}\s+(?:architect|engineer|consultant|director|partner|founder|cto|ceo|cio)\b[^\n]{0,20}"
    r")$",
    re.I,
)

_GREETING_LINE = re.compile(
    r"^(?:"
    r"hi|hello|hey|dear|greetings|good\s+(?:morning|afternoon|evening|day)"
    r")\b"
    r"(?:\s+(?:there|team|all|both|sir|madam|mr\.?|ms\.?|mrs\.?|dr\.?))?"
    r"(?:\s+[A-Za-z][A-Za-z'.\- ]{0,60})?"
    r"\s*[,:;—–-]?\s*$",
    re.I,
)


def strip_email_greeting(body: str, contact_name: str | None = None) -> str:
    """Remove a leading 'Hi Name,' / 'Dear X,' line. The greeting is composed from settings at send time."""
    lines = (body or "").strip().splitlines()
    names = set()
    for n in (contact_name or "").split():
        n = n.strip().lower().rstrip(".,")
        if n:
            names.add(n)
    while lines:
        first = lines[0].strip()
        if not first:
            lines.pop(0)
            continue
        compact = first.lower().rstrip(",:;—–- ").strip()
        if _GREETING_LINE.match(first) or (compact in names):
            lines.pop(0)
            continue
        # "Hi Name, I noticed..." on one line → drop only the greeting prefix.
        m = re.match(
            r"^(?:hi|hello|hey|dear)\s+[A-Za-z][A-Za-z'.\-]{0,40}\s*[,:;—–-]\s+(?=\S)",
            first,
            re.I,
        )
        if not m and names:
            # "Nikolai, managing your book..." — bare vocative using the contact's name.
            alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            m = re.match(rf"^(?:{alts})(?:\s+(?:{alts}))?\s*[,:;—–-]\s+(?=\S)", first, re.I)
        if m:
            lines[0] = first[m.end():].strip()
            if lines[0]:
                lines[0] = lines[0][0].upper() + lines[0][1:]
        break
    return "\n".join(lines).strip()


def strip_email_closings(body: str, extra_names: list[str] | None = None) -> str:
    """Remove trailing Best regards / Best Amir / name-only / title / company lines.

    Sign-off and signature are appended from settings at send time, so the body
    must end with the last sentence of the message.
    """
    names = {n.strip().lower().rstrip(".,") for n in (extra_names or []) if n and n.strip()}
    lines = (body or "").strip().splitlines()
    while lines:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        compact = last.lower().rstrip(".,")
        if compact in names or _CLOSE_LINE.match(last) or _SIGNATURE_TITLE_LINE.match(last):
            lines.pop()
            continue
        # "…question? Best regards, Amir" glued on the last paragraph line.
        m = re.search(
            r"(?:[.?!])\s+(?:best(?:\s+regards|\s+wishes)?|kind\s+regards|warm\s+regards|regards|"
            r"sincerely|thanks(?:\s+again)?|thank\s+you|cheers)\b[^\n]{0,60}$",
            last,
            re.I,
        )
        if m:
            lines[-1] = last[: m.start() + 1].rstrip()
        break
    return "\n".join(lines).strip()


_SENTENCE_SPLIT = re.compile(r"(?<=[.?!])\s+(?=[A-Z])")


def normalize_email_paragraphs(body: str) -> str:
    """2–3 paragraphs with a blank line between them. One dense block gets split."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\n", "\n").strip()
    if not text:
        return ""
    text = re.sub(r"[ \t]+\n", "\n", text)
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    expanded: list[str] = []
    for block in blocks:
        if "\n" in block:
            lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
            if len(lines) >= 2 and all(len(ln) < 220 for ln in lines):
                expanded.extend(lines)
            else:
                expanded.append(" ".join(lines))
        else:
            expanded.append(re.sub(r"[ \t]+", " ", block))
    if len(expanded) >= 2:
        return "\n\n".join(expanded)
    blob = expanded[0] if expanded else text
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(blob) if s.strip()]
    if len(sentences) <= 1:
        return blob
    if len(sentences) <= 3:
        return "\n\n".join(sentences)
    opening = " ".join(sentences[:2])
    closing = sentences[-1]
    middle = " ".join(sentences[2:-1])
    return "\n\n".join(p for p in (opening, middle, closing) if p)


def clean_llm_body(
    body: str,
    contact_name: str | None = None,
    extra_strip: list[str] | None = None,
) -> str:
    """Single entry point: no greeting, no sign-off, no signature — body paragraphs only."""
    settings = load_settings()
    names = [settings.get("sender_name") or ""]
    if names[0]:
        names.append(names[0].split()[0])
    text = (body or "").replace("\r\n", "\n").strip()
    blobs = [settings.get("signature") or "", settings.get("signoff") or ""]
    blobs.extend(extra_strip or [])
    for blob in blobs:
        blob = (blob or "").strip()
        if not blob:
            continue
        text = text.replace(blob, "")
        for line in blob.splitlines():
            line = line.strip()
            if line:
                names.append(line)
                for part in line.split():
                    if len(part) > 2:
                        names.append(part)
    text = strip_email_greeting(text, contact_name)
    text = strip_email_closings(text, names)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return normalize_email_paragraphs(text)


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        if parts:
            return "".join(parts)
    reasoning = message.get("reasoning") or message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return ""


# --------------------------------------------------------------------------- #
# Qwen via LM Studio
# --------------------------------------------------------------------------- #


def list_qwen_models(base_url: str | None = None) -> list[str]:
    settings = load_settings()
    base = (base_url or settings["lmstudio_base_url"]).rstrip("/")
    try:
        res = requests.get(f"{base}/models", timeout=8)
        res.raise_for_status()
        data = res.json()
    except requests.RequestException as exc:
        raise LLMError(f"LM Studio مش واصل على {base}: {exc}") from exc
    models = []
    for item in data.get("data") or []:
        mid = item.get("id")
        if mid:
            models.append(str(mid))
    return models


def call_qwen(prompt: str, max_tokens: int = 800, timeout: int = 180) -> str:
    settings = load_settings()
    base = str(settings["lmstudio_base_url"]).rstrip("/")
    model = str(settings.get("lmstudio_model") or "").strip()
    payload: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "stream": False,
        "max_tokens": max_tokens,
    }
    if model:
        payload["model"] = model
    try:
        res = requests.post(
            f"{base}/chat/completions",
            json=payload,
            headers={"Authorization": "Bearer lm-studio", "Content-Type": "application/json"},
            timeout=max(5, int(timeout)),
        )
    except requests.RequestException as exc:
        raise LLMError(
            f"مقدرناش نكلم LM Studio على {base}. شغّل السيرفر وحمّل موديل Qwen. ({exc})"
        ) from exc
    if res.status_code >= 400:
        raise LLMError(_qwen_http_error(res, model, base))
    data = res.json()
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("LM Studio مرجعش رد.")
    text = _strip_think(_message_text(choices[0].get("message") or {}))
    if not text:
        raise LLMError("رد Qwen فاضي. تأكد إن الموديل محمّل في LM Studio (مش embedding).")
    return text


def _qwen_http_error(res: requests.Response, model: str, base: str) -> str:
    detail = res.text[:400]
    try:
        err = res.json().get("error") or {}
        detail = str(err.get("message") or detail)
    except Exception:
        pass
    if "No models loaded" in detail:
        return (
            "LM Studio السيرفر فاضي. الموديل اللي في شاشة الشات مش كفاية. "
            "من LM Studio: Developer → Local Server → Load لـ qwen3.5-9b. "
            "لو الموديل مفتوح في الشات، اعمله Unload الأول عشان الرام تكفي."
        )
    if "Failed to load model" in detail or res.status_code == 400:
        return (
            f"LM Studio عرف الموديل «{model}» لكنه فشل يحمّله للشات. "
            f"من LM Studio: Load الموديل (Qwen) وبعدين Start Server على {base}. "
            f"التفاصيل: {detail}"
        )
    return f"LM Studio خطأ {res.status_code}: {detail}"


# --------------------------------------------------------------------------- #
# Gemini: model registry, quota memory, rate limiting
# --------------------------------------------------------------------------- #

# Order matters: cheapest/most-available first. Lite models have the most
# generous free quotas and answer fast; they are more than enough for scoring
# leads and writing 60-80 word emails.
GEMINI_CHAT_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
]

GROK_CHAT_MODELS = [
    "grok-4-fast-non-reasoning",
    "grok-4-fast",
    "grok-3-mini",
    "grok-3",
    "grok-2-1212",
]

# Old / retired IDs people still type → a live equivalent.
_GEMINI_ALIASES = {
    "gemini-1.5-flash": "gemini-3.5-flash-lite",
    "gemini-1.5-flash-latest": "gemini-3.5-flash-lite",
    "gemini-1.5-pro": "gemini-3.6-flash",
    "gemini-2.0-flash": "gemini-3.5-flash-lite",
    "gemini-2.0-flash-lite": "gemini-3.5-flash-lite",
    "gemini-2.5-flash": "gemini-3.6-flash",
    "gemini-2.5-flash-lite": "gemini-3.5-flash-lite",
    "gemini-2.5-pro": "gemini-3.6-flash",
    "gemini-pro": "gemini-3.6-flash",
    "gemini-3-8-flash": "gemini-3.8-flash",
    "gemini-3-7-flash": "gemini-3.7-flash",
}

_STATE_LOCK = threading.RLock()
_RATE_LOCK = threading.Lock()
_LAST_REQUEST_TS = 0.0


def normalize_gemini_model(name: str) -> str:
    """Google wants lowercase IDs like gemini-3.5-flash-lite, not display names."""
    raw = (name or "").strip()
    raw = raw.split("?")[0]
    raw = re.sub(r"^https?://generativelanguage\.googleapis\.com/v\d+beta?/models/", "", raw)
    raw = raw.split(":generateContent")[0].strip().strip("/")
    if raw.lower().startswith("models/"):
        raw = raw.split("/", 1)[1]
    compact = re.sub(r"[\s_]+", "-", raw).strip("-").lower()
    if not compact:
        return GEMINI_CHAT_MODELS[0]
    return _GEMINI_ALIASES.get(compact, compact)


def _load_state() -> dict[str, Any]:
    if not LLM_STATE_PATH.exists():
        return {"models": {}, "usage": {}}
    try:
        data = json.loads(LLM_STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError
        data.setdefault("models", {})
        data.setdefault("usage", {})
        return data
    except (json.JSONDecodeError, OSError, ValueError):
        return {"models": {}, "usage": {}}


def _save_state(state: dict[str, Any]) -> None:
    try:
        LLM_STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _next_pacific_midnight_ts() -> float:
    """Google resets free-tier daily quotas at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Los_Angeles")
        now = datetime.now(tz)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return nxt.timestamp()
    except Exception:
        return time.time() + 24 * 3600


def _slot(model: str, key_id: str | None = None) -> str:
    kid = (key_id or "").strip()
    return f"{kid}::{model}" if kid else model


def _block_model(
    model: str,
    seconds: float | None,
    reason: str,
    until_ts: float | None = None,
    key_id: str | None = None,
) -> None:
    with _STATE_LOCK:
        state = _load_state()
        until = until_ts if until_ts is not None else (time.time() + (seconds or 0))
        state["models"][_slot(model, key_id)] = {"blocked_until": until, "reason": reason}
        _save_state(state)


def _record_usage(model: str, key_id: str | None = None) -> None:
    with _STATE_LOCK:
        state = _load_state()
        day = state["usage"].setdefault(_today_key(), {})
        slot = _slot(model, key_id)
        day[slot] = int(day.get(slot, 0)) + 1
        # keep only today + yesterday
        for key in list(state["usage"].keys()):
            if key < (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"):
                state["usage"].pop(key, None)
        _save_state(state)


def _usage_today(model: str, key_id: str | None = None) -> int:
    with _STATE_LOCK:
        state = _load_state()
        return int(state["usage"].get(_today_key(), {}).get(_slot(model, key_id), 0))


def _model_block(model: str, key_id: str | None = None) -> tuple[bool, str, float]:
    with _STATE_LOCK:
        state = _load_state()
        info = state["models"].get(_slot(model, key_id)) or {}
    until = float(info.get("blocked_until") or 0)
    if until > time.time():
        return True, str(info.get("reason") or ""), until
    return False, "", 0.0


def _candidate_models(preferred: str, rotate: bool) -> list[str]:
    ordered = [preferred]
    if rotate:
        for item in GEMINI_CHAT_MODELS:
            if item not in ordered:
                ordered.append(item)
    return ordered


def _rate_limit_wait(rpm: int) -> None:
    global _LAST_REQUEST_TS
    gap = 60.0 / max(1, rpm)
    with _RATE_LOCK:
        now = time.time()
        wait = _LAST_REQUEST_TS + gap - now
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_TS = time.time()


def _parse_429(res: requests.Response) -> tuple[str, float]:
    """Return (kind, retry_seconds). kind ∈ {'day', 'minute', 'unknown'}."""
    kind = "unknown"
    retry = 30.0
    try:
        err = res.json().get("error") or {}
        for det in err.get("details") or []:
            for v in det.get("violations") or []:
                qid = str(v.get("quotaId") or "")
                if "PerDay" in qid:
                    kind = "day"
                elif "PerMinute" in qid and kind != "day":
                    kind = "minute"
            rd = det.get("retryDelay")
            if isinstance(rd, str) and rd.endswith("s"):
                try:
                    retry = float(rd[:-1])
                except ValueError:
                    pass
    except Exception:
        pass
    return kind, retry


def _gemini_error_text(res: requests.Response) -> str:
    try:
        err = res.json().get("error") or {}
        return str(err.get("message") or res.text[:300])
    except Exception:
        return res.text[:300]


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def gemini_status() -> dict[str, Any]:
    """For the dashboard: which models are blocked and how many calls today."""
    settings = load_settings()
    cap = int(settings.get("gemini_daily_cap_per_model") or 0)
    key_id = None
    for cred in llm_keys.available_credentials("gemini"):
        if cred.get("provider") == "gemini":
            key_id = str(cred.get("id") or "") or None
            break
    out: dict[str, Any] = {"models": [], "available": 0}
    for model in GEMINI_CHAT_MODELS:
        blocked, reason, until = _model_block(model, key_id)
        used = _usage_today(model, key_id)
        at_cap = cap > 0 and used >= cap
        out["models"].append(
            {
                "model": model,
                "used_today": used,
                "blocked": blocked or at_cap,
                "reason": reason if blocked else ("الحد اليومي" if at_cap else ""),
                "until": _fmt_time(until) if blocked and until else "",
            }
        )
        if not (blocked or at_cap):
            out["available"] += 1
    return out


def gemini_usage_by_key() -> list[dict[str, Any]]:
    """Today's request counts grouped by API key, then model."""
    cap = int(load_settings().get("gemini_daily_cap_per_model") or 0)
    with _STATE_LOCK:
        day = _load_state().get("usage", {}).get(_today_key(), {})
    rows: list[dict[str, Any]] = []
    for item in llm_keys.list_keys():
        if item.get("provider") != "gemini":
            continue
        kid = str(item.get("id") or "")
        models: list[dict[str, Any]] = []
        total = 0
        for model in GEMINI_CHAT_MODELS:
            used = int(day.get(_slot(model, kid), 0) or 0)
            if not used:
                continue
            blocked, reason, until = _model_block(model, kid)
            at_cap = cap > 0 and used >= cap
            models.append(
                {
                    "model": model,
                    "used": used,
                    "status": "Blocked" if (blocked or at_cap) else "Ready",
                }
            )
            total += used
        rows.append(
            {
                "label": item.get("label") or "Gemini",
                "masked": item.get("masked") or "",
                "blocked": bool(item.get("blocked")),
                "total": total,
                "models": models,
            }
        )
    return rows


def reset_gemini_blocks() -> None:
    with _STATE_LOCK:
        state = _load_state()
        state["models"] = {}
        _save_state(state)


def _gemini_request(model: str, key: str, payload: dict[str, Any], timeout: int) -> requests.Response:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    return requests.post(
        url,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )


def _extract_gemini_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    for cand in data.get("candidates") or []:
        content = cand.get("content") or {}
        for part in content.get("parts") or []:
            if part.get("thought"):
                continue
            if part.get("text"):
                parts.append(part["text"])
    return _strip_think("".join(parts).strip())


def call_gemini(
    prompt: str,
    max_tokens: int = 800,
    json_mode: bool = True,
    rotate: bool | None = None,
    timeout: int = 45,
    max_attempts: int = 2,
    api_key: str | None = None,
    key_id: str | None = None,
) -> str:
    settings = load_settings()
    key = (api_key or env_get("GEMINI_API_KEY") or "").strip()
    if not key:
        raise LLMError("مفتاح Gemini ناقص. حطه من الإعدادات أو .env")

    preferred = normalize_gemini_model(str(settings.get("gemini_model") or GEMINI_CHAT_MODELS[0]))
    if rotate is None:
        rotate = bool(settings.get("gemini_rotate_models", True))
    rpm = int(settings.get("gemini_rpm") or 8)
    cap = int(settings.get("gemini_daily_cap_per_model") or 0)
    attempts = max(1, int(max_attempts))
    timeout = max(5, int(timeout))

    gen: dict[str, Any] = {
        "temperature": 0.3,
        "maxOutputTokens": max_tokens,
    }
    if json_mode:
        gen["responseMimeType"] = "application/json"
    payload: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gen}

    earliest_retry: float | None = None
    skipped: list[str] = []
    last_detail = ""

    for model in _candidate_models(preferred, rotate):
        blocked, reason, until = _model_block(model, key_id)
        if blocked:
            skipped.append(f"{model} ({reason} حتى {_fmt_time(until)})")
            earliest_retry = until if earliest_retry is None else min(earliest_retry, until)
            continue
        if cap > 0 and _usage_today(model, key_id) >= cap:
            until = _next_pacific_midnight_ts()
            skipped.append(f"{model} (الحد اليومي {cap})")
            earliest_retry = until if earliest_retry is None else min(earliest_retry, until)
            continue

        for attempt in range(attempts):
            _rate_limit_wait(rpm)
            try:
                res = _gemini_request(model, key, payload, timeout=timeout)
            except requests.RequestException as exc:
                last_detail = f"{model}: شبكة/مهلة — {type(exc).__name__}: {exc}"
                if attempt < attempts - 1:
                    time.sleep(2)
                    continue
                _block_model(model, 60, "شبكة", key_id=key_id)
                break

            status = res.status_code
            if status < 400:
                _record_usage(model, key_id)
                body = res.json()
                text = _extract_gemini_text(body)
                finish = ""
                try:
                    finish = str((body.get("candidates") or [{}])[0].get("finishReason") or "")
                except Exception:
                    pass
                if finish == "MAX_TOKENS":
                    last_detail = f"{model}: الرد اتقطع (MAX_TOKENS)"
                    if text:
                        # Return the partial JSON so salvage / per-email fallback can finish the job
                        # instead of burning another quota request on a bigger retry.
                        return text
                    if attempt == 0:
                        payload["generationConfig"]["maxOutputTokens"] = min(max(max_tokens * 2, 2048), 8192)
                        continue
                    break
                if text:
                    return text
                last_detail = f"{model}: رد فاضي ({finish or 'بدون سبب'})"
                break

            if status == 404:
                _block_model(model, None, "الموديل متوقف", until_ts=time.time() + 30 * 24 * 3600, key_id=key_id)
                last_detail = f"{model}: 404 {_gemini_error_text(res)[:120]}"
                break

            if status == 429:
                kind, retry = _parse_429(res)
                if kind == "day":
                    until = _next_pacific_midnight_ts()
                    _block_model(model, None, "الحصة اليومية خلصت", until_ts=until, key_id=key_id)
                    last_detail = f"{model}: الحصة اليومية المجانية خلصت"
                    earliest_retry = until if earliest_retry is None else min(earliest_retry, until)
                    break
                wait = min(max(retry, 5.0), 65.0)
                if attempt == 0 and wait <= 30:
                    time.sleep(wait)
                    continue
                _block_model(model, wait, "حد الدقيقة", key_id=key_id)
                last_detail = f"{model}: حد الدقيقة، ارجع بعد {int(wait)} ث"
                break

            if status == 503 or status >= 500:
                if attempt == 0:
                    time.sleep(3)
                    continue
                _block_model(model, 90, "مشغول", key_id=key_id)
                last_detail = f"{model}: مشغول (503)"
                break

            if status == 400:
                cfg = payload["generationConfig"]
                extra_keys = [k for k in list(cfg) if k not in {"temperature", "maxOutputTokens", "responseMimeType"}]
                if extra_keys:
                    for key in extra_keys:
                        cfg.pop(key, None)
                    last_detail = f"{model}: رفض إعداد زيادة — هعيد الطلب"
                    continue
                if "thinking" in res.text.lower() and "thinkingConfig" in cfg:
                    cfg.pop("thinkingConfig", None)
                    continue
                detail = _gemini_error_text(res)
                raise LLMError(f"Gemini خطأ 400 ({model}): {detail[:300]}")

            if status in {401, 403}:
                if key_id:
                    llm_keys.block_key(key_id, "مفتاح Gemini مرفوض")
                raise LLMError("مفتاح Gemini مرفوض. انسخ مفتاح Google AI Studio من aistudio.google.com/apikey.")

            detail = _gemini_error_text(res)
            if "unexpected model name format" in detail.lower():
                raise LLMError(
                    f"اسم موديل Gemini مرفوض: «{model}». اختار موديل من القائمة في الإعدادات."
                )
            raise LLMError(f"Gemini خطأ {status} ({model}): {detail[:300]}")

    # Daily quota on this key — try another key. Do not 48h-lock (that is for billing/credit).
    daily_out = "خلصت" in last_detail or "الحد اليومي" in last_detail or "الحصة اليومية" in last_detail
    if earliest_retry and daily_out:
        raise QuotaExhausted(
            "كل موديلات Gemini المتاحة مستهلكة أو محجوبة دلوقتي. "
            f"أقرب وقت للمحاولة: {_fmt_time(earliest_retry)}. "
            "السيستم هينتقل لمفتاح أو مزود تاني لو موجود."
            + (f" التفاصيل: {last_detail}" if last_detail else ""),
            retry_at=earliest_retry,
        )
    msg = "Gemini فشل بعد التدوير على كل الموديلات."
    if last_detail:
        msg += f" آخر خطأ: {last_detail}"
    if skipped:
        msg += " | متخطّى: " + "، ".join(skipped[:4])
    raise LLMError(msg)


# --------------------------------------------------------------------------- #
# Grok via xAI
# --------------------------------------------------------------------------- #


def call_grok(
    prompt: str,
    max_tokens: int = 800,
    timeout: int = 45,
    api_key: str | None = None,
    key_id: str | None = None,
    json_mode: bool = True,
) -> str:
    key = (api_key or env_get("GROK_API_KEY") or env_get("XAI_API_KEY") or "").strip()
    if not key:
        raise LLMError("مفتاح Grok ناقص.")
    settings = load_settings()
    preferred = str(settings.get("grok_model") or GROK_CHAT_MODELS[0]).strip()
    models = [preferred] + [m for m in GROK_CHAT_MODELS if m != preferred]
    last_detail = ""
    payload_base: dict[str, Any] = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload_base["response_format"] = {"type": "json_object"}
    for model in models:
        blocked, reason, until = _model_block(model, key_id)
        if blocked:
            last_detail = f"{model} محجوب ({reason})"
            continue
        payload = dict(payload_base)
        payload["model"] = model
        try:
            res = requests.post(
                "https://api.x.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=max(5, int(timeout)),
            )
        except requests.RequestException as exc:
            last_detail = f"{model}: شبكة — {exc}"
            continue
        if res.status_code < 400:
            _record_usage(model, key_id)
            data = res.json()
            text = ""
            try:
                text = str((((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or "")
            except Exception:
                text = ""
            text = _strip_think(text.strip())
            if text:
                return text
            last_detail = f"{model}: رد فاضي"
            continue
        detail = ""
        try:
            detail = str((res.json().get("error") or {}).get("message") or res.text[:300])
        except Exception:
            detail = res.text[:300]
        if res.status_code in {401, 403}:
            if key_id:
                llm_keys.block_key(key_id, "مفتاح Grok مرفوض")
            raise LLMError(f"مفتاح Grok مرفوض ({model}): {detail[:200]}")
        if res.status_code == 429 or llm_keys.is_credit_error(detail):
            if llm_keys.is_credit_error(detail) or "credit" in detail.lower() or "billing" in detail.lower():
                _block_model(model, None, "كريدت", until_ts=time.time() + llm_keys.BLOCK_SECONDS, key_id=key_id)
                if key_id:
                    llm_keys.block_key(key_id, "كريدت Grok خلص")
                raise QuotaExhausted(f"Grok خلص الكريدت على {model}.")
            _block_model(model, 60, "حد الدقيقة", key_id=key_id)
            last_detail = f"{model}: حد الدقيقة"
            continue
        if res.status_code == 404:
            _block_model(model, None, "الموديل مش موجود", until_ts=time.time() + 30 * 24 * 3600, key_id=key_id)
            last_detail = f"{model}: 404"
            continue
        last_detail = f"{model}: خطأ {res.status_code} {detail[:160]}"
    raise LLMError(f"Grok فشل. آخر خطأ: {last_detail or '—'}")


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #


def call_llm(prompt: str, max_tokens: int = 800, json_mode: bool = True) -> str:
    settings = load_settings()
    provider = str(settings.get("llm_provider") or "auto")
    errors: list[str] = []
    if provider == "qwen":
        try:
            return call_qwen(prompt, max_tokens=max_tokens)
        except LLMError as exc:
            errors.append(str(exc))
    creds = llm_keys.available_credentials(preferred=provider)
    if not creds and provider != "qwen":
        gem = env_get("GEMINI_API_KEY")
        if gem:
            creds = [{"id": "env-gemini", "provider": "gemini", "key": gem, "label": "Gemini"}]
        grok = env_get("GROK_API_KEY") or env_get("XAI_API_KEY")
        if grok:
            creds.append({"id": "env-grok", "provider": "grok", "key": grok, "label": "Grok"})
    for cred in creds:
        cid = str(cred.get("id") or "")
        try:
            if cred.get("provider") == "gemini":
                text = call_gemini(
                    prompt,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                    api_key=str(cred.get("key") or ""),
                    key_id=cid,
                )
            elif cred.get("provider") == "grok":
                text = call_grok(
                    prompt,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                    api_key=str(cred.get("key") or ""),
                    key_id=cid,
                )
            else:
                continue
            if cid:
                llm_keys.touch_key(cid)
            return text
        except QuotaExhausted as exc:
            if cid and llm_keys.is_credit_error(str(exc)):
                llm_keys.block_key(cid, "الكريدت خلص")
            errors.append(str(exc))
            continue
        except LLMError as exc:
            msg = str(exc)
            if cid and llm_keys.is_credit_error(msg):
                llm_keys.block_key(cid, "الكريدت خلص")
            errors.append(msg)
            continue
    if provider != "qwen":
        try:
            return call_qwen(prompt, max_tokens=max_tokens)
        except LLMError as exc:
            errors.append(str(exc))
    raise LLMError("مفيش مفتاح ذكاء متاح دلوقتي. " + " | ".join(errors[:4]))


def test_llm() -> str:
    """Ping the configured brain. Does not require JSON — a non-empty reply is enough."""
    settings = load_settings()
    provider = str(settings.get("llm_provider") or "auto")
    text = call_llm("Reply with the single word pong and nothing else.", max_tokens=32, json_mode=False)
    text = (text or "").strip()
    if not text:
        raise LLMError("العقل رد فاضي. المفتاح أو الموديل مش شغال.")
    if provider == "qwen":
        return f"Qwen شغال ({settings.get('lmstudio_model') or 'الموديل المحمّل'})."
    live = llm_keys.list_keys()
    open_n = sum(1 for k in live if not k.get("blocked"))
    return f"العقل شغال ({provider}) · رد: {text[:60]} · {open_n} مفتاح متاح."


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _coerce_score(value: Any) -> int | None:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(1, min(10, score))


def qualify_lead(company_name: str, title: str, notes: str, contact_name: str = "") -> dict[str, Any]:
    """Single-lead qualification (kept for re-checks). Prefer qualify_leads_batch."""
    settings = load_settings()
    prompt = fill_prompt(
        settings["qualify_prompt"],
        icp=settings.get("icp") or "",
        company_name=company_name or "(unknown company)",
        contact_name=contact_name or "(unknown)",
        title=title or "(unknown title)",
        notes=notes or "(no notes provided)",
        min_score=settings["qualify_min_score"],
    )
    data = extract_json(call_llm(with_json_contract(prompt, JSON_CONTRACT_QUALIFY), max_tokens=700))
    score = _coerce_score(data.get("score"))
    if score is None:
        raise LLMError("التقييم مفيهوش score رقمي.")
    reasoning = str(data.get("reasoning") or "").strip()
    hook_raw = data.get("hook")
    hook = None if hook_raw in (None, "", "null") else str(hook_raw).strip()
    if score < int(settings["qualify_min_score"]):
        hook = None
    return {"score": score, "reasoning": reasoning, "hook": hook}


def qualify_leads_batch(leads: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Score many leads in ONE request. Returns {lead_id: {score, reasoning, hook}}.

    Leads missing from the model's answer are simply absent from the result so
    the caller can retry them later; nothing is guessed.
    """
    if not leads:
        return {}
    settings = load_settings()
    min_score = int(settings["qualify_min_score"])
    icp = (settings.get("icp") or "").strip()

    lines = []
    for lead in leads:
        lines.append(
            f"- id={int(lead['id'])} | Company: {_clip(lead.get('company_name') or '(unknown)', 80)} | "
            f"Contact: {_clip(lead.get('contact_name') or '(unknown)', 60)} | "
            f"Title: {_clip(lead.get('title') or '(unknown)', 90)} | "
            f"Notes: {_clip((lead.get('source_notes') or '(none)').replace(chr(10), ' ; '), 700)}"
        )

    prompt = (
        "You are helping a solo quantitative-trading-systems architect decide which leads deserve a "
        "personalized cold email.\n\n"
        f"Ideal customer profile:\n{icp}\n\n"
        "For EACH lead below:\n"
        f"1. Score fit from 1-10. Be conservative: most leads should score below {min_score} unless there is "
        "clear evidence of systematic / quantitative trading activity (hedge fund, prop desk, CTA, family "
        "office running systematic strategies, algorithmic execution).\n"
        "2. Give 1-2 sentences of reasoning grounded ONLY in the provided fields.\n"
        f"3. If score >= {min_score}, write one specific, honest personalization hook based only on the notes. "
        "Otherwise hook must be null. Never invent facts.\n\n"
        f"Leads ({len(leads)}):\n" + "\n".join(lines)
    )
    prompt = with_json_contract(prompt, JSON_CONTRACT_QUALIFY_BATCH)

    max_tokens = 350 * len(leads) + 300
    raw = call_llm(prompt, max_tokens=max_tokens)
    data = extract_json_any(raw)
    items: Any
    if isinstance(data, dict):
        items = data.get("results") or data.get("leads") or data.get("data") or []
    else:
        items = data
    if not isinstance(items, list):
        raise LLMError("رد التقييم مش قائمة.")

    wanted = {int(lead["id"]) for lead in leads}
    out: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            lid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if lid not in wanted:
            continue
        score = _coerce_score(item.get("score"))
        if score is None:
            continue
        reasoning = str(item.get("reasoning") or "").strip()
        hook_raw = item.get("hook")
        hook = None if hook_raw in (None, "", "null") else str(hook_raw).strip()
        if score < min_score:
            hook = None
        out[lid] = {"score": score, "reasoning": reasoning, "hook": hook}
    if not out:
        raise LLMError("التقييم الجماعي رجع من غير نتائج مطابقة للـ ids.")
    return out


_SAME_FIRM_ANGLES = [
    "Angle A: offer a technical case study and backtest audit, tied to this person's own hook.",
    "Angle B: risk-governance and operational controls for a live book. Do not repeat a case-study offer if a colleague's email already used it.",
    "Angle C: multi-account copy / execution operations on cTrader or MT5. End with a different question.",
    "Angle D: one short, specific question about their current stack. No repeated proof points from a colleague's email.",
]


def _last_sent_block(n: int = 3, kind: str = "initial") -> str:
    """Fill {last_3_sent_emails}. Empty list is a short note, never a crash."""
    try:
        import db as _db

        rows = _db.get_recent_sent_emails(n, kind=kind)
    except Exception:
        rows = []
    if not rows:
        return "(none yet)"
    chunks: list[str] = []
    for i, row in enumerate(rows, start=1):
        who = _clip(row.get("contact_name") or "—", 40)
        firm = _clip(row.get("company_name") or "—", 50)
        subj = _clip(row.get("subject") or "", 100)
        body = _clip((row.get("body") or "").strip(), 900)
        chunks.append(f"{i}. To {who} at {firm}\nSubject: {subj}\n{body}")
    return "\n\n".join(chunks)


def _draft_prompt_vars(**overrides: object) -> dict[str, object]:
    settings = load_settings()
    values: dict[str, object] = {
        "sender_name": settings["sender_name"],
        "sender_title": settings["sender_title"],
        "contact_name": "there",
        "company_name": "your firm",
        "hook": "(no specific hook — stay generic and honest, do not invent facts)",
        "offer": settings.get("offer") or "",
        "word_min": settings["email_word_min"],
        "word_max": settings["email_word_max"],
        "signature": settings.get("signature") or "",
        "last_3_sent_emails": _last_sent_block(3, kind="initial"),
    }
    values.update(overrides)
    return values


_BATCH_APPLY = (
    "\n\nApply the instructions above to EACH recipient separately. "
    "Use that row's name, company, title, and hook. Write one distinct email per id. "
    "Do not write a greeting, sign-off, or signature — the system adds those.\n"
)
_PARAGRAPH_RULE = (
    "\n\nLayout: 2 or 3 short paragraphs in the body JSON, separated by a blank line "
    "(hook, then the offer, then a short question). Never one dense block."
)
_BATCH_SAME_FIRM = (
    "\nSAME-FIRM RULE: Recipients who share same_firm_code are colleagues at one company. "
    "Their emails MUST differ in subject, opening, proof point, and closing question. "
    "Never mention the other colleague.\n"
)


def _same_firm_block(sibling_emails: list[dict[str, Any]] | None, variant_index: int = 0) -> str:
    if not sibling_emails and variant_index <= 0:
        return ""
    parts = [
        "\n\nSAME-FIRM RULE: Another person at this same company is also in the outreach. "
        "This email MUST be clearly different: different subject, different opening sentence, "
        "different proof point, and a different closing question. "
        "Never mention that you emailed (or will email) a colleague.",
        "Use this angle: " + _SAME_FIRM_ANGLES[int(variant_index) % len(_SAME_FIRM_ANGLES)],
    ]
    if sibling_emails:
        parts.append("Do NOT reuse wording from these colleague emails:")
        for i, sib in enumerate(sibling_emails[:3], start=1):
            parts.append(
                f"{i}. To {_clip(sib.get('name') or 'colleague', 40)} "
                f"({_clip(sib.get('title') or '', 40)}): "
                f"subject={_clip(sib.get('subject') or '', 80)}; "
                f"body={_clip((sib.get('body') or '').replace(chr(10), ' '), 420)}"
            )
    return "\n".join(parts)


def draft_email(
    contact_name: str,
    company_name: str,
    hook: str,
    sibling_emails: list[dict[str, Any]] | None = None,
    variant_index: int = 0,
) -> dict[str, str]:
    settings = load_settings()
    prompt = fill_prompt(
        settings["draft_prompt"],
        **_draft_prompt_vars(
            contact_name=contact_name or "there",
            company_name=company_name or "your firm",
            hook=hook or "(no specific hook — stay generic and honest, do not invent facts)",
        ),
    )
    prompt += _same_firm_block(sibling_emails, variant_index)
    prompt += _PARAGRAPH_RULE
    data = extract_json(call_llm(with_json_contract(prompt, JSON_CONTRACT_EMAIL), max_tokens=DRAFT_MAX_TOKENS))
    return _parse_email_json(data, contact_name)


def draft_emails_batch(leads: list[dict[str, Any]]) -> dict[int, dict[str, str]]:
    """Write several first-touch emails in ONE request, using the settings draft prompt."""
    if not leads:
        return {}
    settings = load_settings()
    lines = []
    codes = {}
    try:
        import db as _db

        codes = _db.company_code_map(leads)
    except Exception:
        codes = {}
    for lead in leads:
        hook = (lead.get("suggested_hook") or "").strip() or "(no specific hook — stay generic and honest)"
        code = codes.get(int(lead["id"])) or ""
        tag = f" | same_firm_code={code}" if code else ""
        lines.append(
            f"- id={int(lead['id'])} | To: {_clip(lead.get('contact_name') or 'there', 60)} at "
            f"{_clip(lead.get('company_name') or 'your firm', 80)} | Title: {_clip(lead.get('title') or '', 80)} | "
            f"Hook: {_clip(hook, 400)}{tag}"
        )
    prompt = fill_prompt(
        settings["draft_prompt"],
        **_draft_prompt_vars(
            contact_name="EACH recipient in Recipients below",
            company_name="THAT recipient's company in Recipients below",
            hook="THAT recipient's hook in Recipients below",
        ),
    )
    prompt += _BATCH_APPLY
    prompt += _PARAGRAPH_RULE
    if any(codes.values()):
        prompt += _BATCH_SAME_FIRM
    prompt += f"\nRecipients ({len(leads)}):\n" + "\n".join(lines)
    prompt = with_json_contract(prompt, JSON_CONTRACT_EMAIL_BATCH)

    max_tokens = min(DRAFT_MAX_TOKENS, max(4096, 2200 * len(leads) + 800))
    raw = call_llm(prompt, max_tokens=max_tokens)
    try:
        data = extract_json_any(raw)
    except LLMError as exc:
        raise LLMError("Gemini قطع رد الكتابة قبل ما يخلّص JSON.") from exc
    items = data.get("emails") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise LLMError("رد الدرافتات مش قائمة.")
    wanted = {int(lead["id"]) for lead in leads}
    by_id = {int(lead["id"]): (lead.get("contact_name") or "") for lead in leads}
    out: dict[int, dict[str, str]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            lid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if lid not in wanted:
            continue
        try:
            out[lid] = _parse_email_json(item, by_id.get(lid))
        except LLMError:
            continue
    if not out:
        raise LLMError("الدرافت الجماعي رجع من غير إيميلات صالحة.")
    return out


def draft_followup(
    contact_name: str,
    company_name: str,
    hook: str,
    original_subject: str,
    original_body: str,
    step: int,
    sibling_emails: list[dict[str, Any]] | None = None,
    variant_index: int = 0,
) -> dict[str, str]:
    settings = load_settings()
    if int(step) >= 3:
        template = str(settings.get("followup_3_prompt") or settings.get("followup_prompt") or "")
    elif int(step) >= 2:
        template = str(settings.get("followup_2_prompt") or settings.get("followup_prompt") or "")
    else:
        template = str(settings.get("followup_prompt") or "")
    prompt = fill_prompt(
        template,
        sender_name=settings["sender_name"],
        sender_title=settings["sender_title"],
        contact_name=contact_name or "there",
        company_name=company_name or "your firm",
        hook=hook or "",
        original_subject=original_subject or "",
        original_body=original_body or "",
        step=step,
        word_min=settings["email_word_min"],
        word_max=settings["email_word_max"],
        last_3_sent_emails=_last_sent_block(3, kind="initial"),
        drive_folder_url=str(settings.get("drive_folder_url") or settings.get("drive_case_study_url") or "").strip(),
    )
    prompt += _same_firm_block(sibling_emails, variant_index)
    prompt += _PARAGRAPH_RULE
    data = extract_json(call_llm(with_json_contract(prompt, JSON_CONTRACT_EMAIL), max_tokens=DRAFT_MAX_TOKENS))
    return _parse_email_json(data, contact_name)


def arabic_review_copy(subject: str, body: str) -> dict[str, str]:
    """Arabic copy for the sender's eyes only. Never sent."""
    prompt = (
        "Translate this English cold email into clear Modern Standard Arabic "
        "for the sender's internal review. Keep the meaning. Do not add a greeting or signature.\n\n"
        f"Subject:\n{subject}\n\nBody:\n{body}"
    )
    data = extract_json(call_llm(with_json_contract(prompt, JSON_CONTRACT_AR), max_tokens=AR_MAX_TOKENS))
    return {
        "subject": str(data.get("subject") or "").strip(),
        "body": str(data.get("body") or "").strip(),
    }


def translate_notes_ar(items: list[tuple[int, str]]) -> dict[int, str]:
    """Translate English notes to Arabic. Keeps specifics; does not summarize them away."""
    batch = [(int(i), (t or "").strip()) for i, t in items if (t or "").strip()]
    if not batch:
        return {}
    lines = [f"id={i}\n{text[:900]}" for i, text in batch]
    prompt = (
        "Translate each English rejection note into clear Arabic. "
        "Keep every specific fact, name, and reason. Do not replace the note with a generic sentence.\n\n"
        + "\n\n".join(lines)
    )
    data = extract_json(call_llm(with_json_contract(prompt, JSON_CONTRACT_TRANSLATE), max_tokens=2200))
    out: dict[int, str] = {}
    rows = data.get("items") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return out
    wanted = {i for i, _ in batch}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            lid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        ar = str(row.get("ar") or "").strip()
        if lid in wanted and ar:
            out[lid] = ar
    return out


def _parse_email_json(data: dict[str, Any], contact_name: str | None = None) -> dict[str, str]:
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    if not subject or not body:
        raise LLMError("الدرافت ناقص subject أو body.")
    body = body.replace("!", ".")
    body = clean_llm_body(body, contact_name)
    if not body:
        raise LLMError("الدرافت طلع فاضي بعد شيل التحية والختام.")
    return {"subject": subject, "body": body}
