"""Deterministic pre-filter. Zero API calls.

Purpose: do not spend a scarce LLM request on a lead that has no trace of
systematic / quantitative trading anywhere in its title, company, or Apollo
keywords. Conservative on purpose: anything with a real signal goes to the LLM.
"""

from __future__ import annotations

import re
from typing import Any

# Any of these in title/company/notes = plausible fit → let the LLM decide.
SIGNAL_TERMS = [
    "quant",
    "quantitative",
    "systematic",
    "algorithmic",
    "algo trading",
    "algo-trading",
    "trading",
    "trader",
    "hedge fund",
    "hedge-fund",
    "prop desk",
    "proprietary trading",
    "prop trading",
    "cta",
    "family office",
    "asset management",
    "asset manager",
    "investment management",
    "portfolio manager",
    "portfolio management",
    "execution",
    "market making",
    "market maker",
    "hft",
    "high frequency",
    "high-frequency",
    "derivatives",
    "futures",
    "options trading",
    "fx trading",
    "forex",
    "ctrader",
    "mt5",
    "metatrader",
    "backtest",
    "strategy development",
    "capital management",
    "capital partners",
    "investment firm",
    "trading firm",
    "trading systems",
    "trading bot",
    "multi-strategy",
    "risk management",
    "fintech",
    "financial services",
    "investments",
]

# Titles that almost never commission bespoke trading systems.
NON_BUYER_TITLES = [
    "recruiter",
    "talent",
    "human resources",
    "hr ",
    "marketing",
    "content",
    "social media",
    "sales development",
    "sdr",
    "bdr",
    "account executive",
    "customer success",
    "support",
    "intern",
    "student",
    "assistant",
    "receptionist",
    "office manager",
    "accountant",
    "bookkeeper",
    "designer",
    "copywriter",
    "community manager",
]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def evaluate(lead: dict[str, Any]) -> tuple[str, str]:
    """Return ("llm", "") or ("reject", reason)."""
    title = _norm(lead.get("title") or "")
    company = _norm(lead.get("company_name") or "")
    notes = _norm(lead.get("source_notes") or "")
    blob = " ".join([title, company, notes])

    has_signal = any(term in blob for term in SIGNAL_TERMS)
    non_buyer = any(term in f" {title} " for term in NON_BUYER_TITLES)

    if non_buyer and not any(term in title for term in ("founder", "ceo", "cio", "cto", "partner", "head", "director", "chief")):
        return "reject", "فلتر سريع: المنصب مش دور مشتري لأنظمة التداول"
    if not has_signal:
        return "reject", "فلتر سريع: مفيش إشارة تداول منظّم أو إدارة أصول في المنصب أو الشركة"
    return "llm", ""
