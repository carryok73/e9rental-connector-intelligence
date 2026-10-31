"""Read-only WhatsApp Cloud API webhook handling.

Meta's message envelope is recognized, while group identity is deliberately a
configured path because group payload details depend on the organizer's setup.
"""
import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from ..db import upsert_record


def verify_token(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(str(provided), str(expected))


def verify_signature(raw_body: bytes, signature: str | None, app_secret: str) -> bool:
    """Validate Meta's X-Hub-Signature-256 HMAC over the exact request bytes."""
    if not app_secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _path_value(value: Any, path: str) -> Any:
    """Read a dot-separated path from a message; empty paths fail closed."""
    if not path:
        return None
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
        if current is None:
            return None
    return current


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def interpret_message(text: str) -> dict[str, Any]:
    """Extract conservative, deterministic interpretation candidates locally."""
    sentences = _sentences(text)
    request_patterns = re.compile(r"\b(please|request(?:ing)?|need(?:ed)?|could you|can you|would you|will provide|will send|commit(?:ted)?|agreed to)\b", re.I)
    action_patterns = re.compile(r"\b(please|todo|to do|follow up|send|call|check|confirm|prepare|review|schedule|deliver)\b", re.I)
    decision_patterns = re.compile(r"\b(decided|decision|approved|rejected|we will proceed|agreed|confirmed)\b", re.I)
    return {
        "summary": " ".join(sentences[:2])[:500],
        "requests_and_commitments": [s for s in sentences if request_patterns.search(s)],
        "action_items": [s for s in sentences if action_patterns.search(s)],
        "decisions": [s for s in sentences if decision_patterns.search(s)],
        "method": "local keyword and sentence heuristics; candidates require human review",
    }


def _message_events(payload: Mapping[str, Any]):
    """Yield standard Cloud API message objects; status updates are ignored."""
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            value = change.get("value")
            if not isinstance(value, Mapping):
                continue
            messages = value.get("messages", [])
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, Mapping):
                        yield message


def normalize_selected_messages(payload: Mapping[str, Any], selected_groups: set[str],
                                group_id_path: str) -> list[dict[str, Any]]:
    """Normalize only selected messages; group filtering precedes content reads."""
    if not selected_groups or not group_id_path:
        return []
    normalized: list[dict[str, Any]] = []
    for message in _message_events(payload):
        raw_group = _path_value(message, group_id_path)
        # bool is an int subclass, but it cannot be a meaningful group ID.
        if isinstance(raw_group, bool) or not isinstance(raw_group, (str, int)):
            continue
        group_id = str(raw_group)
        if not group_id.strip():
            continue
        if group_id not in selected_groups:
            continue

        # Content and sender fields are accessed only after the strict group gate.
        source_id = message.get("id")
        if not isinstance(source_id, str) or not source_id:
            continue
        message_type = message.get("type")
        text = ""
        if message_type == "text":
            body = message.get("text")
            if isinstance(body, Mapping) and isinstance(body.get("body"), str):
                text = body["body"]
        elif isinstance(message_type, str):
            # Meta media messages can carry a user-authored caption.
            media = message.get(message_type)
            if isinstance(media, Mapping) and isinstance(media.get("caption"), str):
                text = media["caption"]
        sender = message.get("from")
        timestamp = message.get("timestamp")
        normalized.append({
            "group_id": group_id,
            "source_id": source_id,
            "text": text,
            "timestamp": str(timestamp) if timestamp is not None else None,
            "metadata": {
                "sender": str(sender) if sender is not None else None,
                "message_type": str(message_type) if message_type is not None else None,
                "interpretation": interpret_message(text) if text else interpret_message(""),
            },
        })
    return normalized


def ingest_event(conn: sqlite3.Connection, payload: Mapping[str, Any], selected_groups: set[str],
                 mapper: Callable[[Mapping[str, Any]], list[dict[str, Any]]]) -> int:
    """Persist mapper-produced records only after group selection; deduplicate by source ID."""
    accepted = 0
    for item in mapper(payload):
        group_id = str(item.get("group_id", ""))
        if group_id not in selected_groups:
            continue
        upsert_record(conn, source="whatsapp", source_id=str(item["source_id"]),
                      title=str(item.get("title", "WhatsApp message")), content=str(item.get("text", "")),
                      occurred_at=item.get("timestamp"), parent_id=group_id,
                      metadata_json=json.dumps(item.get("metadata", {}), ensure_ascii=False))
        accepted += 1
    conn.commit()
    return accepted


def ingest_cloud_api_event(conn: sqlite3.Connection, payload: Mapping[str, Any],
                           selected_groups: set[str], group_id_path: str) -> int:
    records = normalize_selected_messages(payload, selected_groups, group_id_path)
    return ingest_event(conn, payload, selected_groups, lambda _: records)
