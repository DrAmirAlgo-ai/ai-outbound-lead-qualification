"""Apollo people search + enrichment. Uses the official API only (no scraping)."""

from __future__ import annotations

from typing import Any

import requests

from settings_store import env_get, lines_to_list, load_settings

BASE = "https://api.apollo.io/api/v1"


class ApolloError(RuntimeError):
    pass


def _headers() -> dict[str, str]:
    key = env_get("APOLLO_API_KEY")
    if not key:
        raise ApolloError("مفتاح Apollo ناقص. حطه من الإعدادات.")
    return {
        "x-api-key": key,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "accept": "application/json",
    }


def configured() -> bool:
    from settings_store import env_configured

    return env_configured("APOLLO_API_KEY")


def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    url = f"{BASE}{path}"
    try:
        res = requests.request(method, url, headers=_headers(), timeout=60, **kwargs)
    except requests.RequestException as exc:
        raise ApolloError(f"فشل الاتصال بـ Apollo: {exc}") from exc
    if res.status_code == 401:
        raise ApolloError("مفتاح Apollo مرفوض. تأكد إنه Master API key.")
    if res.status_code == 403:
        raise ApolloError(
            "Apollo رفض الطلب (403). غالباً الخطة أو المفتاح مالوش People Search. "
            "استخدم endpoint api_search ومفتاح Master."
        )
    if res.status_code == 429:
        raise ApolloError("Apollo عمل rate limit. استنى شوية وحاول تاني.")
    if res.status_code >= 400:
        raise ApolloError(f"Apollo خطأ {res.status_code}: {res.text[:400]}")
    try:
        return res.json()
    except ValueError as exc:
        raise ApolloError("Apollo مرجعش JSON.") from exc


def search_people(page: int = 1, per_page: int = 25) -> dict[str, Any]:
    settings = load_settings()
    payload: dict[str, Any] = {
        "page": page,
        "per_page": min(max(per_page, 1), 100),
        "include_similar_titles": bool(settings.get("apollo_include_similar_titles", True)),
    }
    titles = lines_to_list(settings.get("apollo_titles") or "")
    if titles:
        payload["person_titles"] = titles
    seniorities = lines_to_list(settings.get("apollo_seniorities") or "")
    if seniorities:
        payload["person_seniorities"] = seniorities
    locations = lines_to_list(settings.get("apollo_locations") or "")
    if locations:
        payload["person_locations"] = locations
    org_kw = lines_to_list(settings.get("apollo_org_keywords") or "")
    if org_kw:
        payload["q_organization_keyword_tags"] = org_kw
    ranges = lines_to_list(settings.get("apollo_employee_ranges") or "")
    if ranges:
        payload["organization_num_employees_ranges"] = ranges
    keywords = (settings.get("apollo_keywords") or "").strip()
    if keywords:
        payload["q_keywords"] = keywords
    return _request("POST", "/mixed_people/api_search", json=payload)


def enrich_people(person_ids: list[str]) -> list[dict[str, Any]]:
    """Reveal work emails via people match. Batches of 10."""
    cleaned = [pid for pid in person_ids if pid]
    matches: list[dict[str, Any]] = []
    for i in range(0, len(cleaned), 10):
        chunk = cleaned[i : i + 10]
        data = _request(
            "POST",
            "/people/bulk_match",
            json={
                "details": [{"id": pid} for pid in chunk],
                "reveal_personal_emails": False,
                "reveal_phone_number": False,
            },
        )
        batch = data.get("matches") or data.get("people") or []
        if batch:
            matches.extend([m for m in batch if isinstance(m, dict)])
            continue
        for pid in chunk:
            single = _request("POST", "/people/match", json={"id": pid, "reveal_personal_emails": False})
            person = single.get("person") or single
            if isinstance(person, dict):
                matches.append(person)
    return matches


def _org(person: dict[str, Any]) -> dict[str, Any]:
    org = person.get("organization") or person.get("account") or {}
    return org if isinstance(org, dict) else {}


def person_to_lead(person: dict[str, Any]) -> dict[str, str]:
    org = _org(person)
    first = (person.get("first_name") or "").strip()
    last = (person.get("last_name") or "").strip()
    name = (person.get("name") or f"{first} {last}").strip()
    email = (
        person.get("email")
        or person.get("work_email")
        or ""
    )
    if isinstance(email, dict):
        email = email.get("email") or ""
    email = str(email or "").strip()
    notes_parts = []
    for label, value in [
        ("headline", person.get("headline")),
        ("seniority", person.get("seniority")),
        ("city", person.get("city") or person.get("present_raw_address")),
        ("country", person.get("country")),
        ("company", org.get("name")),
        ("industry", org.get("industry")),
        ("keywords", ", ".join(org.get("keywords") or []) if isinstance(org.get("keywords"), list) else org.get("keywords")),
        ("short_description", org.get("short_description") or org.get("seo_description")),
        ("linkedin", person.get("linkedin_url") or person.get("linkedin_url")),
    ]:
        if value:
            notes_parts.append(f"{label}: {value}")
    return {
        "apollo_id": str(person.get("id") or "").strip(),
        "company_name": str(org.get("name") or person.get("organization_name") or "").strip(),
        "contact_name": name,
        "contact_email": email,
        "title": str(person.get("title") or "").strip(),
        "linkedin_url": str(person.get("linkedin_url") or "").strip(),
        "organization_domain": str(org.get("primary_domain") or org.get("website_url") or "").strip(),
        "source_notes": "\n".join(notes_parts),
    }


def email_usable(email: str) -> bool:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    if any(ch in email for ch in "*•"):
        return False
    if "email_not_unlocked" in email or email.endswith("@domain.com") and "not" in email:
        return False
    if email in {"unavailable", "not_unlocked@apollo.io"}:
        return False
    return True


def fetch_leads(max_leads: int | None = None) -> dict[str, Any]:
    """Search + enrich until we have up to max_leads people with usable emails."""
    settings = load_settings()
    target = int(max_leads or settings["apollo_max_per_run"] or 20)
    target = max(1, min(target, 100))
    collected: list[dict[str, Any]] = []
    skipped_no_email = 0
    page = 1
    pages_scanned = 0
    raw_people = 0

    while len(collected) < target and page <= 10:
        result = search_people(page=page, per_page=min(25, target * 2))
        people = result.get("people") or result.get("contacts") or []
        pages_scanned += 1
        if not people:
            break
        raw_people += len(people)
        ids = [str(p.get("id")) for p in people if p.get("id")]
        enriched = enrich_people(ids)
        by_id = {str(p.get("id")): p for p in people}
        for person in enriched:
            pid = str(person.get("id") or "")
            merged = {**by_id.get(pid, {}), **person}
            mapped = person_to_lead(merged)
            if not email_usable(mapped["contact_email"]):
                skipped_no_email += 1
                continue
            collected.append(mapped)
            if len(collected) >= target:
                break
        page += 1

    return {
        "leads": collected[:target],
        "raw_people": raw_people,
        "pages_scanned": pages_scanned,
        "skipped_no_email": skipped_no_email,
    }
