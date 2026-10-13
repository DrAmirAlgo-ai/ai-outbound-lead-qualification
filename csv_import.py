"""CSV import from Apollo exports or a manual list. No APIs, no scraping."""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

from db import email_exists, extract_linkedin_url, insert_lead, update_linkedin_if_empty

COMPANY_COLS = ["company_name", "company", "company name", "organization", "account", "account name"]
FIRST_COLS = ["first name", "first_name", "firstname"]
LAST_COLS = ["last name", "last_name", "lastname"]
NAME_COLS = ["contact_name", "contact name", "name", "full name", "full_name", "contact"]
EMAIL_COLS = ["contact_email", "email", "email address", "work email", "email_address"]
TITLE_COLS = ["title", "job title", "job_title", "position", "headline"]
NOTES_COLS = ["notes", "source_notes", "note", "keywords", "seo description"]
EXTRA_NOTE_COLS = [
    "industry",
    "sub-industry",
    "# employees",
    "employees",
    "keywords",
    "city",
    "country",
    "linkedin url",
    "person linkedin url",
    "company linkedin url",
    "technologies",
    "annual revenue",
]


def _norm(col: str) -> str:
    return str(col).strip().lower()


def _first_match(columns: list[str], candidates: list[str]) -> str | None:
    lookup = {_norm(c): c for c in columns}
    for cand in candidates:
        if cand in lookup:
            return lookup[cand]
    return None


def _cell(row: pd.Series, col: str | None) -> str:
    if not col or col not in row.index:
        return ""
    val = row[col]
    if pd.isna(val):
        return ""
    return str(val).strip()


def _linkedin_from_row(row: pd.Series, columns: list[str]) -> str:
    person = ""
    company = ""
    for col in columns:
        name = _norm(col)
        if "linkedin" not in name:
            continue
        val = _cell(row, col)
        if not val:
            continue
        if "company" in name or "organization" in name:
            company = company or val
        else:
            person = person or val
    return extract_linkedin_url(person, company)


def parse_csv(file_bytes: bytes) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return pd.read_csv(io.BytesIO(file_bytes), encoding=encoding, dtype=str).fillna("")
        except UnicodeDecodeError:
            continue
    return pd.read_csv(io.BytesIO(file_bytes), encoding="latin-1", dtype=str).fillna("")


def parse_table(file_bytes: bytes, filename: str = "") -> pd.DataFrame:
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(file_bytes), dtype=str).fillna("")
    return parse_csv(file_bytes)


def map_row(row: pd.Series, columns: list[str]) -> dict[str, str]:
    company_col = _first_match(columns, COMPANY_COLS)
    email_col = _first_match(columns, EMAIL_COLS)
    title_col = _first_match(columns, TITLE_COLS)
    notes_col = _first_match(columns, NOTES_COLS)
    name_col = _first_match(columns, NAME_COLS)
    first_col = _first_match(columns, FIRST_COLS)
    last_col = _first_match(columns, LAST_COLS)

    if name_col:
        contact_name = _cell(row, name_col)
    else:
        contact_name = " ".join(p for p in [_cell(row, first_col), _cell(row, last_col)] if p).strip()

    note_parts: list[str] = []
    primary_notes = _cell(row, notes_col)
    if primary_notes:
        note_parts.append(primary_notes)
    for extra in EXTRA_NOTE_COLS:
        col = _first_match(columns, [extra])
        val = _cell(row, col)
        if val and val not in primary_notes:
            note_parts.append(f"{extra}: {val}")
    for col in columns:
        name = _norm(col)
        if "linkedin" not in name:
            continue
        val = _cell(row, col)
        if val and val not in "\n".join(note_parts):
            note_parts.append(f"{name}: {val}")

    return {
        "company_name": _cell(row, company_col),
        "contact_name": contact_name,
        "contact_email": _cell(row, email_col),
        "title": _cell(row, title_col),
        "notes": "\n".join(note_parts),
        "linkedin_url": _linkedin_from_row(row, columns),
    }


def import_csv(file_bytes: bytes, filename: str = "") -> dict[str, Any]:
    df = parse_table(file_bytes, filename)
    columns = list(df.columns)
    inserted = 0
    skipped_dupes = 0
    skipped_invalid = 0
    skipped_filled = 0
    errors: list[str] = []

    for idx, row in df.iterrows():
        mapped = map_row(row, columns)
        email = mapped["contact_email"]
        if not email or "@" not in email:
            skipped_invalid += 1
            continue
        if email_exists(email):
            skipped_dupes += 1
            if mapped.get("linkedin_url") and update_linkedin_if_empty(email, mapped["linkedin_url"]):
                skipped_filled += 1
            continue
        try:
            insert_lead(
                mapped["company_name"],
                mapped["contact_name"],
                email,
                mapped["title"],
                mapped["notes"],
                linkedin_url=mapped.get("linkedin_url") or "",
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001 — surface row-level import errors
            errors.append(f"Row {int(idx) + 2}: {exc}")

    return {
        "inserted": inserted,
        "skipped_dupes": skipped_dupes,
        "skipped_filled": skipped_filled,
        "skipped_invalid": skipped_invalid,
        "errors": errors,
        "rows_in_file": len(df),
    }


def inspect_file(file_bytes: bytes, filename: str = "") -> dict[str, Any]:
    """Column-mapping health check. Does not write to the database."""
    df = parse_table(file_bytes, filename)
    columns = list(df.columns)
    name_col = _first_match(columns, NAME_COLS) or _first_match(columns, FIRST_COLS)
    return {
        "rows": int(len(df)),
        "columns": [str(c) for c in columns],
        "mapped": {
            "Name": name_col or "",
            "Email": _first_match(columns, EMAIL_COLS) or "",
            "Company": _first_match(columns, COMPANY_COLS) or "",
            "Title": _first_match(columns, TITLE_COLS) or "",
        },
    }
