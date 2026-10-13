"""Persistent follow-up attachments (PDF pack for step 2, one video for step 3)."""

from __future__ import annotations

import re
from pathlib import Path

from settings_store import persist_dir

PDF_MAX_TOTAL = 13 * 1024 * 1024
VIDEO_MAX = 26 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _root() -> Path:
    path = persist_dir() / "attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pdf_dir() -> Path:
    path = _root() / "followup_2_pdfs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def video_path() -> Path | None:
    folder = _root() / "followup_3_video"
    folder.mkdir(parents=True, exist_ok=True)
    files = [p for p in folder.iterdir() if p.is_file()]
    return files[0] if files else None


def _safe_name(name: str) -> str:
    base = Path(name or "file").name
    cleaned = _SAFE.sub("_", base).strip("._") or "file"
    return cleaned[:120]


def list_pdfs() -> list[dict[str, object]]:
    rows = []
    for path in sorted(pdf_dir().iterdir()):
        if path.is_file():
            rows.append({"name": path.name, "path": path, "size": path.stat().st_size})
    return rows


def pdf_total_bytes() -> int:
    return sum(int(item["size"]) for item in list_pdfs())


def pdf_paths() -> list[Path]:
    return [item["path"] for item in list_pdfs()]  # type: ignore[misc]


def save_pdf(name: str, data: bytes) -> None:
    blob = data or b""
    if pdf_total_bytes() + len(blob) > PDF_MAX_TOTAL:
        raise ValueError("حجم ملفات PDF هيتعدى 13 ميجا.")
    dest = pdf_dir() / _safe_name(name)
    dest.write_bytes(blob)


def delete_pdf(name: str) -> None:
    path = pdf_dir() / Path(name).name
    if path.is_file() and path.resolve().parent == pdf_dir().resolve():
        path.unlink()


def save_video(name: str, data: bytes) -> None:
    blob = data or b""
    if len(blob) > VIDEO_MAX:
        raise ValueError("الفيديو أكبر من 26 ميجا.")
    folder = _root() / "followup_3_video"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if old.is_file():
            old.unlink()
    (folder / _safe_name(name)).write_bytes(blob)


def delete_video() -> None:
    path = video_path()
    if path and path.is_file():
        path.unlink()


def attachments_for_kind(kind: str) -> list[Path]:
    """Nothing is attached. Follow-up 2 carries one Drive folder link in the body."""
    return []
