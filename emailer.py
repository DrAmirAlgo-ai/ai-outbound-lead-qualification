"""SMTP sending via Zoho (or any SMTP). Nothing sends unless called from Dispatch."""

from __future__ import annotations

import html
import mimetypes
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from settings_store import env_get, load_env


class SendError(RuntimeError):
    pass


def smtp_status() -> dict[str, object]:
    load_env()
    user = env_get("SMTP_USER")
    password = env_get("SMTP_PASSWORD")
    host = env_get("SMTP_HOST", "smtp.zoho.com")
    return {
        "host": host,
        "port": env_get("SMTP_PORT", "465"),
        "user": user,
        "from_addr": env_get("SMTP_FROM") or user,
        "password_configured": bool(password),
        "ready": bool(host and user and password),
    }


def _build_message(
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
) -> EmailMessage:
    from_addr = env_get("SMTP_FROM") or env_get("SMTP_USER")
    if not from_addr:
        raise SendError("SMTP_FROM / SMTP_USER is missing.")
    domain = from_addr.split("@", 1)[1] if "@" in from_addr else None
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain)
    chain = [item.strip() for item in (references or []) if item and str(item).strip()]
    parent = (in_reply_to or "").strip()
    if parent and parent not in chain:
        chain.append(parent)
    if not parent and chain:
        parent = chain[-1]
    if parent:
        # In-Reply-To is the latest message. References keeps the whole chain
        # so follow-up 2 stays in the same thread as the opening email and follow-up 1.
        msg["In-Reply-To"] = parent
        msg["References"] = " ".join(chain)
    msg.set_content(body, charset="utf-8")
    msg.add_alternative(_plain_to_html(body), subtype="html")
    return msg


def _plain_to_html(plain: str) -> str:
    """Gmail and Outlook render HTML; paragraph tags keep the blank lines visible."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", (plain or "").strip()) if b.strip()]
    if not blocks:
        return "<div></div>"
    parts: list[str] = []
    for i, block in enumerate(blocks):
        inner = html.escape(block, quote=True).replace("\n", "<br>\n")
        margin = "0 0 1.15em 0" if i < len(blocks) - 1 else "0"
        parts.append(f'<p style="margin:{margin};line-height:1.55;">{inner}</p>')
    return (
        '<div style="font-family:Georgia,\'Times New Roman\',serif;font-size:15px;'
        'line-height:1.55;color:#222;">'
        + "".join(parts)
        + "</div>"
    )


def _connect() -> smtplib.SMTP:
    load_env()
    host = env_get("SMTP_HOST", "smtp.zoho.com")
    port = int(env_get("SMTP_PORT", "465") or 465)
    user = env_get("SMTP_USER")
    password = env_get("SMTP_PASSWORD")
    use_ssl = env_get("SMTP_USE_SSL", "true").lower() in {"1", "true", "yes"}
    if not host or not user or not password:
        raise SendError("SMTP host, user, or password is missing.")

    context = ssl.create_default_context()
    if use_ssl or port == 465:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, context=context, timeout=180)
    else:
        smtp = smtplib.SMTP(host, port, timeout=180)
        smtp.starttls(context=context)
    smtp.login(user, password)
    return smtp


def test_connection() -> str:
    try:
        smtp = _connect()
        try:
            smtp.noop()
        finally:
            smtp.quit()
    except SendError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface SMTP failures in the UI
        raise SendError(str(exc)) from exc
    return "SMTP login succeeded."


def send_email(
    to_addr: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    attachments: list[Path] | None = None,
    references: list[str] | None = None,
) -> str:
    """Send and return the Message-ID that was put on the wire."""
    if not to_addr or "@" not in to_addr:
        raise SendError(f"Invalid recipient: {to_addr!r}")
    msg = _build_message(to_addr, subject, body, in_reply_to=in_reply_to, references=references)
    for path in attachments or []:
        if not path or not path.is_file():
            continue
        ctype, _enc = mimetypes.guess_type(path.name)
        if not ctype:
            ctype = "application/octet-stream"
        main, sub = ctype.split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
    try:
        smtp = _connect()
        try:
            smtp.send_message(msg)
        finally:
            smtp.quit()
    except SendError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface SMTP failures in the UI
        raise SendError(str(exc)) from exc
    return str(msg["Message-ID"])
