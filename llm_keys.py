"""Multiple cloud LLM API keys with 48-hour credit lockout and round-robin."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

from settings_store import env_get, persist_dir, _atomic_write_text

BLOCK_SECONDS = 48 * 3600
_LOCK = threading.RLock()


def keys_path():
    return persist_dir() / "llm_keys.json"


def _empty() -> dict[str, Any]:
    return {"keys": []}


def _load() -> dict[str, Any]:
    path = keys_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _empty()
        data.setdefault("keys", [])
        return data
    except (json.JSONDecodeError, OSError):
        return _empty()


def _save(data: dict[str, Any]) -> None:
    _atomic_write_text(keys_path(), json.dumps(data, indent=2, ensure_ascii=False))


def _mask(key: str) -> str:
    raw = (key or "").strip()
    if len(raw) <= 8:
        return "••••"
    return raw[:4] + "…" + raw[-4:]


def _seed_from_env(data: dict[str, Any]) -> bool:
    if data.get("env_seeded"):
        return False
    existing = {(str(k.get("provider")), str(k.get("key"))) for k in data.get("keys") or []}
    gem = env_get("GEMINI_API_KEY")
    if gem and ("gemini", gem) not in existing:
        data["keys"].append(_new_key("gemini", gem, "Gemini (.env)"))
        existing.add(("gemini", gem))
    grok = env_get("GROK_API_KEY") or env_get("XAI_API_KEY")
    if grok and ("grok", grok) not in existing:
        data["keys"].append(_new_key("grok", grok, "Grok (.env)"))
    data["env_seeded"] = True
    return True


def _new_key(provider: str, key: str, label: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:10],
        "provider": provider,
        "key": key.strip(),
        "label": (label or provider).strip(),
        "blocked_until": 0.0,
        "block_reason": "",
        "last_used": 0.0,
    }


def list_keys() -> list[dict[str, Any]]:
    with _LOCK:
        data = _load()
        if _seed_from_env(data):
            _save(data)
        now = time.time()
        out = []
        for item in data.get("keys") or []:
            until = float(item.get("blocked_until") or 0)
            blocked = until > now
            out.append(
                {
                    "id": item.get("id"),
                    "provider": item.get("provider"),
                    "label": item.get("label") or item.get("provider"),
                    "masked": _mask(str(item.get("key") or "")),
                    "blocked": blocked,
                    "blocked_until": until if blocked else 0,
                    "block_reason": item.get("block_reason") or "",
                    "last_used": float(item.get("last_used") or 0),
                }
            )
        return out


def add_key(provider: str, key: str, label: str = "") -> str:
    provider = (provider or "").strip().lower()
    if provider not in {"gemini", "grok"}:
        raise ValueError("المزود لازم يكون gemini أو grok")
    key = (key or "").strip()
    if not key:
        raise ValueError("المفتاح فاضي")
    with _LOCK:
        data = _load()
        _seed_from_env(data)
        item = _new_key(provider, key, label or provider)
        data["keys"].append(item)
        _save(data)
        return str(item["id"])


def remove_key(key_id: str) -> None:
    with _LOCK:
        data = _load()
        data["keys"] = [k for k in data.get("keys") or [] if str(k.get("id")) != str(key_id)]
        _save(data)


def block_key(key_id: str, reason: str = "الكريدت خلص", seconds: int = BLOCK_SECONDS) -> None:
    with _LOCK:
        data = _load()
        until = time.time() + max(60, int(seconds))
        for item in data.get("keys") or []:
            if str(item.get("id")) == str(key_id):
                item["blocked_until"] = until
                item["block_reason"] = reason
        _save(data)


def touch_key(key_id: str) -> None:
    with _LOCK:
        data = _load()
        for item in data.get("keys") or []:
            if str(item.get("id")) == str(key_id):
                item["last_used"] = time.time()
        _save(data)


def available_credentials(preferred: str = "auto") -> list[dict[str, Any]]:
    """Unblocked keys, preferred provider first, least-recently-used inside a provider."""
    with _LOCK:
        data = _load()
        if _seed_from_env(data):
            _save(data)
        now = time.time()
        live = []
        for item in data.get("keys") or []:
            until = float(item.get("blocked_until") or 0)
            if until > now:
                continue
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            live.append(item)
        live.sort(key=lambda k: float(k.get("last_used") or 0))
        prefer = (preferred or "auto").lower()
        if prefer in {"gemini", "grok"}:
            first = [k for k in live if k.get("provider") == prefer]
            rest = [k for k in live if k.get("provider") != prefer]
            live = first + rest
        return live


def is_credit_error(text: str) -> bool:
    low = (text or "").lower()
    return any(
        m in low
        for m in (
            "credit",
            "insufficient funds",
            "insufficient_quota",
            "billing",
            "payment required",
            "الكريدت",
            "الفوترة",
        )
    )
