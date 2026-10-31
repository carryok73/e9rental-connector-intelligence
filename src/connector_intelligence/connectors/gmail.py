"""Read-only Gmail API connector and deterministic message processing."""
import base64
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from ..db import upsert_record
from ..exports import export_source

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = [GMAIL_SCOPE]


class GmailConnectorError(RuntimeError):
    """A safe, payload-free error from Gmail setup or API operations."""


_CLASSIFIERS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("marketing", ("unsubscribe", "newsletter", "campaign", "promotion", "webinar"),
     "Contains a marketing signal such as a newsletter, campaign, or unsubscribe notice."),
    ("sales", ("quote", "quotation", "purchase", "rental", "booking", "pricing", "lead"),
     "Contains a sales signal such as a quote, rental, booking, pricing, or lead request."),
)

_ATTACHMENT_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("invoice", re.compile(r"(?<![a-z0-9])invoices?(?![a-z0-9])", re.I), "Filename indicates an invoice."),
    ("purchase_order", re.compile(r"(?<![a-z0-9])purchase[\s_-]*orders?(?![a-z0-9])|(?<![a-z0-9])p\.?\s*o\.?\s*(?:#|number|no\.?\s*)?\d+(?![a-z0-9])|(?<![a-z0-9])po(?![a-z0-9])", re.I),
     "Filename indicates a purchase order."),
    ("quotation", re.compile(r"(?<![a-z0-9])quotations?(?![a-z0-9])|(?<![a-z0-9])quotes?(?![a-z0-9])", re.I), "Filename indicates a quotation or quote."),
    ("contract", re.compile(r"(?<![a-z0-9])contracts?(?![a-z0-9])|(?<![a-z0-9])agreements?(?![a-z0-9])", re.I), "Filename indicates a contract or agreement."),
    ("verification", re.compile(r"\bkyc\b|\bverification\b|\bidentity[\s_-]*(?:document|verification|proof)\b|\bproof[\s_-]*of[\s_-]*(?:address|identity)\b", re.I),
     "Filename indicates a KYC or identity verification document."),
)


def classify_email_with_reason(subject: str, body: str) -> tuple[str, str]:
    """Return exactly one deterministic category and a brief matching rationale."""
    text = f"{subject}\n{body}".casefold()
    for category, signals, reason in _CLASSIFIERS:
        if any(signal in text for signal in signals):
            return category, reason
    return "other", "No configured sales or marketing signal was found."


def classify_email(subject: str, body: str) -> str:
    """Compatibility wrapper returning exactly sales, marketing, or other."""
    return classify_email_with_reason(subject, body)[0]


def attachment_flags(attachments: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Flag likely critical files using filename tokens, avoiding substring matches."""
    result = []
    for attachment in attachments:
        filename = str(attachment.get("filename", "")).strip()
        for category, pattern, reason in _ATTACHMENT_PATTERNS:
            if filename and pattern.search(filename):
                result.append({"filename": filename, "category": category, "reason": reason})
                break
    return result


def _normalize_address(value: Any) -> str:
    parsed = parseaddr(str(value))[1]
    return (parsed or str(value)).strip().casefold()


def unanswered_inbound(thread_messages: list[dict[str, Any]], reference_date: date, days: int = 5) -> list[dict[str, Any]]:
    """Return old inbound messages without any later owner message in their thread.

    The owner's addresses are supplied on normalized records as ``_own_addresses``.
    Each inbound is evaluated independently: any later owner message in the same
    thread counts as a reply, regardless of how many earlier inbound messages exist.
    The cutoff is inclusive: a message dated exactly ``reference_date - days`` qualifies.
    """
    if days < 0:
        raise ValueError("days must be non-negative")
    parsed: list[tuple[datetime, int, dict[str, Any], str, str]] = []
    for index, message in enumerate(thread_messages):
        try:
            when = datetime.fromtimestamp(int(message["internalDate"]) / 1000, timezone.utc)
        except (KeyError, ValueError, TypeError, OverflowError, OSError):
            try:
                when = parsedate_to_datetime(message["date"])
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                else:
                    when = when.astimezone(timezone.utc)
            except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
                continue
        sender = _normalize_address(message.get("from", ""))
        if not sender:
            # Without a sender we cannot safely distinguish an inbound from an owner reply.
            continue
        raw_thread_id = message.get("threadId")
        # The function's ordinary caller already passes one thread at a time. Preserve
        # that behavior for normalized fixtures without threadId, while separating IDs
        # whenever Gmail supplied them.
        thread_id = str(raw_thread_id) if raw_thread_id else "__implicit_thread__"
        parsed.append((when, index, message, sender, thread_id))

    own_addresses = {
        _normalize_address(address)
        for message in thread_messages
        for address in ([message["_own_addresses"]] if isinstance(message.get("_own_addresses"), str)
                        else message.get("_own_addresses", []))
        if _normalize_address(address)
    }
    cutoff = reference_date - timedelta(days=days)
    owner_replies_by_thread: dict[str, list[datetime]] = defaultdict(list)
    for when, _, _, sender, thread_id in parsed:
        if sender in own_addresses:
            owner_replies_by_thread[thread_id].append(when)

    unanswered: list[tuple[datetime, int, dict[str, Any]]] = []
    for when, index, message, sender, thread_id in parsed:
        if sender in own_addresses or when.date() > cutoff:
            continue
        if any(reply_time > when for reply_time in owner_replies_by_thread.get(thread_id, [])):
            continue
        unanswered.append((when, index, message))
    unanswered.sort(key=lambda item: (item[0], item[1]))
    return [message for _, _, message in unanswered]


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        return value


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def normalize_gmail_message(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert one Gmail API ``format=full`` message into a source-neutral record."""
    payload = raw.get("payload") or {}
    headers = {
        str(item.get("name", "")).casefold(): _decode_header(item.get("value"))
        for item in payload.get("headers", [])
        if item.get("name")
    }
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    def visit(part: dict[str, Any]) -> None:
        filename = _decode_header(part.get("filename"))
        body_part = part.get("body") or {}
        mime_type = str(part.get("mimeType", ""))
        if filename or body_part.get("attachmentId"):
            attachment: dict[str, Any] = {
                "filename": filename,
                "mime_type": mime_type,
                "size": body_part.get("size"),
                "attachment_id": body_part.get("attachmentId"),
            }
            attachments.append(attachment)
        elif mime_type == "text/plain":
            text = _decode_body(body_part.get("data"))
            if text:
                plain_parts.append(text)
        elif mime_type == "text/html":
            text = _decode_body(body_part.get("data"))
            if text:
                parser = _HTMLText()
                parser.feed(text)
                html_parts.append(" ".join(parser.parts))
        for child in part.get("parts", []) or []:
            visit(child)

    visit(payload)
    body = "\n".join(plain_parts or html_parts)

    def addresses(header_name: str) -> list[str]:
        return [address.casefold() for _, address in getaddresses([headers.get(header_name, "")]) if address]

    sender_values = addresses("from")
    recipients = addresses("to") + addresses("cc") + addresses("bcc")
    internal_date = raw.get("internalDate")
    occurred_at = None
    try:
        if internal_date is not None:
            occurred_at = datetime.fromtimestamp(int(internal_date) / 1000, timezone.utc).isoformat()
        elif headers.get("date"):
            parsed_date = parsedate_to_datetime(headers["date"])
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)
            occurred_at = parsed_date.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        occurred_at = None

    subject = headers.get("subject", "")
    category, category_reason = classify_email_with_reason(subject, body)
    flags = attachment_flags(attachments)
    return {
        "id": str(raw.get("id", "")),
        "threadId": str(raw.get("threadId", "")),
        "from": sender_values[0] if sender_values else "",
        "to": recipients,
        "subject": subject,
        "date": occurred_at,
        "internalDate": str(internal_date) if internal_date is not None else None,
        "body": body,
        "attachments": attachments,
        "attachment_flags": flags,
        "category": category,
        "category_reason": category_reason,
        "message_id_header": headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
    }


def _execute(request: Any, operation: str) -> dict[str, Any]:
    try:
        result = request.execute()
    except Exception as exc:
        # API exception bodies can contain user data. Keep them out of logs and UI errors.
        raise GmailConnectorError(f"Gmail API request failed during {operation}.") from exc
    if not isinstance(result, dict):
        raise GmailConnectorError(f"Gmail API returned an invalid response during {operation}.")
    return result


def fetch_gmail_messages(service: Any, page_size: int = 100) -> list[dict[str, Any]]:
    """Fetch all non-spam/trash message records through Gmail list and thread APIs."""
    if not 1 <= page_size <= 500:
        raise ValueError("page_size must be between 1 and 500")
    listed: dict[str, str] = {}
    page_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        args: dict[str, Any] = {"userId": "me", "maxResults": page_size, "includeSpamTrash": False}
        if page_token:
            args["pageToken"] = page_token
        response = _execute(service.users().messages().list(**args), "messages.list")
        for item in response.get("messages", []) or []:
            message_id, thread_id = item.get("id"), item.get("threadId")
            if message_id:
                listed[str(message_id)] = str(thread_id or "")
        next_token = response.get("nextPageToken")
        if not next_token:
            break
        next_token = str(next_token)
        if next_token in seen_tokens:
            raise GmailConnectorError("Gmail API pagination returned a repeated page token.")
        seen_tokens.add(next_token)
        page_token = next_token

    thread_ids = list(dict.fromkeys(thread_id for thread_id in listed.values() if thread_id))
    results: list[dict[str, Any]] = []
    retrieved: set[str] = set()
    for thread_id in thread_ids:
        response = _execute(service.users().threads().get(userId="me", id=thread_id, format="full"), "threads.get")
        for raw in response.get("messages", []) or []:
            message_id = str(raw.get("id", ""))
            # Only ingest messages returned by the mailbox-wide, non-spam/trash listing.
            if message_id in listed and message_id not in retrieved:
                results.append(normalize_gmail_message(raw))
                retrieved.add(message_id)

    for message_id, thread_id in listed.items():
        if message_id in retrieved:
            continue
        raw = _execute(service.users().messages().get(userId="me", id=message_id, format="full"), "messages.get")
        raw.setdefault("threadId", thread_id)
        results.append(normalize_gmail_message(raw))
    results.sort(key=lambda item: (item.get("date") or "", item["id"]))
    return results


def get_gmail_service(settings: Any) -> Any:
    """Create an official Gmail API client using the single readonly scope."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailConnectorError("Google API dependencies are not installed in this environment.") from exc

    client_secrets = Path(settings.google_client_secrets_file)
    token_path = Path(settings.google_token_file)
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES) if token_path.exists() else None
        if credentials and credentials.scopes and set(credentials.scopes) != {GMAIL_SCOPE}:
            raise GmailConnectorError("The saved Google token has scopes beyond gmail.readonly; reauthorize with a readonly-only token.")
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), GMAIL_SCOPES)
                credentials = flow.run_local_server(port=0)
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(credentials.to_json(), encoding="utf-8")
        if credentials.scopes and set(credentials.scopes) != {GMAIL_SCOPE}:
            raise GmailConnectorError("Google authorization did not grant only gmail.readonly.")
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)
    except GmailConnectorError:
        raise
    except Exception as exc:
        # Do not relay OAuth exceptions because they may include local paths or token details.
        raise GmailConnectorError("Gmail OAuth setup failed. Check the local client secrets and token configuration.") from exc


def _mailbox_profile(service: Any) -> str:
    response = _execute(service.users().getProfile(userId="me"), "users.getProfile")
    email_address = str(response.get("emailAddress", "")).strip().casefold()
    if not email_address:
        raise GmailConnectorError("Gmail profile did not return a mailbox address.")
    return email_address


def store_message(conn: sqlite3.Connection, message: dict[str, Any]) -> None:
    """Upsert one normalized message with classification and source metadata."""
    category, reason = classify_email_with_reason(str(message.get("subject", "")), str(message.get("body", "")))
    metadata = {
        "sender": message.get("from", ""),
        "recipients": message.get("to", []),
        "attachments": message.get("attachments", []),
        "attachment_flags": message.get("attachment_flags", []),
        "is_inbound": message.get("is_inbound"),
        "is_unanswered": bool(message.get("is_unanswered", False)),
        "unanswered_reference_date": message.get("unanswered_reference_date"),
        "unanswered_days": message.get("unanswered_days"),
        "classification_reason": reason,
        "message_id_header": message.get("message_id_header", ""),
        "in_reply_to": message.get("in_reply_to", ""),
    }
    metadata.update(message.get("metadata", {}))
    content = "\n".join(part for part in (str(message.get("subject", "")), str(message.get("body", ""))) if part)
    upsert_record(
        conn,
        source="gmail",
        source_id=str(message["id"]),
        parent_id=message.get("threadId"),
        occurred_at=message.get("date"),
        title=str(message.get("subject", "")),
        content=content,
        category=category,
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )


def sync_gmail(
    conn: sqlite3.Connection,
    service: Any,
    *,
    mailbox_owner: str,
    reference_date: date,
    output_dir: str | Path = "outputs",
    days: int = 5,
    page_size: int = 100,
) -> dict[str, int]:
    """Fetch the configured mailbox, classify/store messages, and refresh its export."""
    expected_owner = _normalize_address(mailbox_owner)
    if not expected_owner:
        raise GmailConnectorError("Set GMAIL_MAILBOX_OWNER to the authorized sandbox mailbox before syncing.")
    actual_owner = _mailbox_profile(service)
    if actual_owner != expected_owner:
        raise GmailConnectorError("The authorized Gmail account does not match GMAIL_MAILBOX_OWNER; no messages were fetched.")

    messages = fetch_gmail_messages(service, page_size=page_size)
    threads: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in messages:
        message["_own_addresses"] = [expected_owner]
        message["is_inbound"] = _normalize_address(message.get("from", "")) != expected_owner
        threads[str(message.get("threadId", ""))].append(message)

    unanswered_ids: set[str] = set()
    for thread_messages in threads.values():
        unanswered_ids.update(
            str(message["id"])
            for message in unanswered_inbound(thread_messages, reference_date, days)
        )
    for message in messages:
        message["is_unanswered"] = str(message["id"]) in unanswered_ids
        message["unanswered_reference_date"] = reference_date.isoformat()
        message["unanswered_days"] = days

    with conn:
        for message in messages:
            store_message(conn, message)
    export_source(conn, "gmail", Path(output_dir) / "gmail_output.json")
    return {
        "messages_fetched": len(messages),
        "threads_fetched": len(threads),
        "unanswered_inbound": len(unanswered_ids),
        "important_attachments": sum(len(message.get("attachment_flags", [])) for message in messages),
    }
