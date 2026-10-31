"""Deterministic extraction and evidence summaries for verification checklists."""
from __future__ import annotations

import re
from typing import Any

from .rental_quote import _locations_for_span, _read_locations


_STATUS_LABELS = (
    ("rejected", re.compile(r"\brejected\b", re.I)),
    ("failed", re.compile(r"\bfailed\b", re.I)),
    ("pending", re.compile(r"\bpending\b|\bawaiting\b|\bin review\b", re.I)),
    ("verified", re.compile(r"\bverified\b", re.I)),
    ("unknown", re.compile(r"\bunknown\b|\bnot reviewed\b|\bnot checked\b", re.I)),
)
_DOCUMENTS_HEADER = re.compile(r"^\s*document\s*(?:type|name)?\s*$", re.I)
_STATUS_HEADER = re.compile(r"^\s*(?:verification\s+)?status\s*$", re.I)


def is_verification_question(question: str) -> bool:
    """Recognize simple KYC/customer document-verification questions."""
    value = question.casefold()
    if re.search(r"\b(?:rental|campaign|budget|invoice|payment|whatsapp|gmail)\b", value):
        return False
    requested_status = r"(?:verified|pending|rejected|failed|status)"
    return bool(
        re.search(r"\b(?:customer|kyc|verification)\b.*\b(?:status|details)\b", value)
        or re.search(r"\b(?:show|list|find)\b.*\b(?:customer\s+)?verification\b", value)
        or re.search(rf"\bdocuments?\b.*\b{requested_status}\b", value)
        or re.search(rf"\bdriving\s+licen[cs]e\b.*\b{requested_status}\b", value)
        or re.search(rf"\bproof\s+of\s+address\b.*\b{requested_status}\b", value)
        or re.search(r"\b(?:all|requirements?)\b.*\b(?:completed|complete|verified)\b", value)
    )


def _split_cells(line: str, line_start: int) -> list[tuple[str, int, int]]:
    cells = []
    offset = 0
    for raw in line.split("|"):
        left = len(raw) - len(raw.lstrip())
        right = len(raw.rstrip())
        cells.append((raw.strip(), line_start + offset + left, line_start + offset + right))
        offset += len(raw) + 1
    return cells


def _status(value: str) -> tuple[str, tuple[int, int] | None]:
    if re.search(r"\bnot\s+(?:verified|pending|rejected|failed)\b", value, re.I):
        return "unknown", None
    for label, pattern in _STATUS_LABELS:
        match = pattern.search(value)
        if match:
            if label == "verified" and re.search(r"\b(?:not|un)\s+verified\b", value, re.I):
                continue
            return label, match.span()
    return "unknown", None


def is_verification_document(content: str) -> bool:
    """Require verification context plus a document/status table in body content."""
    context = bool(re.search(
        r"\b(?:customer\s+kyc\s+verification|customer\s+verification|verification\s+checklist)\b",
        content, re.I,
    ))
    has_document_header = bool(re.search(r"document\s*(?:type|name)\s*\|", content, re.I))
    has_status_header = bool(re.search(r"\|\s*verification\s+status\s*\|", content, re.I))
    return context and has_document_header and has_status_header


def extract_verification_records(content: str, metadata_json: Any = "{}") -> dict[str, Any]:
    """Extract checklist entries, explicit statuses, notes, spans, and line citations."""
    locations = _read_locations(metadata_json, len(content))
    entries: list[dict[str, Any]] = []
    header: list[str] | None = None
    columns: dict[str, int | None] = {}
    offset = 0
    for raw_line in content.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        line_start, line_end = offset, offset + len(line)
        offset += len(raw_line)
        if not line.strip() or "|" not in line:
            continue
        cells = _split_cells(line, line_start)
        values = [item[0] for item in cells]
        if _DOCUMENTS_HEADER.search(values[0]) and any(_STATUS_HEADER.search(value) for value in values[1:]):
            header = values
            columns = {
                "name": 0,
                "status": next((index for index, value in enumerate(values)
                                if _STATUS_HEADER.search(value)), None),
                "notes": next((index for index, value in enumerate(values)
                               if re.fullmatch(r"\s*(?:notes?|comments?|reason)\s*", value, re.I)), None),
            }
            continue
        if header is None or columns.get("status") is None:
            continue
        if len(cells) < len(header):
            cells.extend([("", line_end, line_end)] * (len(header) - len(cells)))
        name = cells[0][0]
        status_cell = cells[columns["status"]]
        if not name:
            continue
        normalized_status, status_span = _status(status_cell[0])
        note_index = columns.get("notes")
        note_cell = cells[note_index] if note_index is not None else ("", line_end, line_end)
        row_locations = _locations_for_span(line_start, line_end, locations)
        status_locations = _locations_for_span(status_cell[1], status_cell[2], locations)
        entries.append({
            "document_name": name,
            "status": normalized_status,
            "status_text": status_cell[0],
            "status_start": status_cell[1] + status_span[0] if status_span else None,
            "status_end": status_cell[1] + status_span[1] if status_span else None,
            "status_evidence": status_cell[0] if status_span else None,
            "notes": note_cell[0] or None,
            "notes_start": note_cell[1] if note_cell[0] else None,
            "notes_end": note_cell[2] if note_cell[0] else None,
            "evidence": line.strip(),
            "start": line_start,
            "end": line_end,
            "source_locations": row_locations,
            "status_source_locations": status_locations,
            "location_status": "mapped" if row_locations else "unavailable",
        })

    disclaimer = re.search(
        r"do not treat (?:these )?sample statuses as real customer verification\.?",
        content, re.I,
    )
    return {
        "records": entries,
        "is_verification_document": is_verification_document(content),
        "source_locations_available": bool(locations),
        "source_note": ({
            "text": "These are synthetic sample statuses and the source says not to treat them as real customer verification.",
            "evidence": content[disclaimer.start():disclaimer.end()],
            "start": disclaimer.start(), "end": disclaimer.end(),
            "source_locations": _locations_for_span(disclaimer.start(), disclaimer.end(), locations),
        } if disclaimer else None),
    }


def requested_verification_view(question: str) -> str:
    """Choose a document-list, specific-document, or overall-status view."""
    value = question.casefold()
    if re.search(r"\b(?:all|overall|requirements?|completed|complete|status)\b", value):
        if re.search(r"\b(?:all|requirements?|completed|complete)\b", value):
            return "overall"
    if re.search(r"driving\s+licen[cs]e", value):
        return "driving_licence"
    if re.search(r"proof\s+of\s+address", value):
        return "proof_of_address"
    if re.search(r"\b(?:pending|awaiting|in review)\b", value):
        return "pending"
    if re.search(r"\b(?:rejected|failed)\b", value):
        return "action_required"
    if re.search(r"\bverified\b", value):
        return "verified"
    return "details"


def overall_verification_status(records: list[dict[str, Any]], content: str) -> dict[str, Any]:
    """Return a bounded conclusion without assuming the checklist is exhaustive."""
    if not records:
        return {"status": "unable_to_determine", "basis": "No document-status rows were extracted.",
                "source_locations": []}
    rejected = [item for item in records if item["status"] in {"rejected", "failed"}]
    if rejected:
        locations = [location for item in rejected for location in item["source_locations"]]
        return {"status": "action_required", "basis": "At least one listed document was explicitly rejected or failed.",
                "source_locations": _unique_locations(locations)}
    missing = [item for item in records if item["status"] in {"unknown", ""}]
    if missing:
        return {"status": "unable_to_determine", "basis": "One or more listed documents have no recognized status.",
                "source_locations": _unique_locations(
                    location for item in missing for location in item["source_locations"]
                )}
    pending = [item for item in records if item["status"] == "pending"]
    if pending:
        locations = [location for item in pending for location in item["source_locations"]]
        return {
            "status": "incomplete",
            "basis": (f"{len(pending)} listed document check(s) are explicitly pending. "
                      "The document does not establish that the checklist includes every requirement."),
            "source_locations": _unique_locations(locations),
        }
    exhaustive = bool(re.search(
        r"\b(?:all\s+(?:required\s+)?(?:documents?|checks?|requirements?)\s+"
        r"(?:are\s+)?(?:listed|included|verified|complete|completed)|"
        r"checklist\s+(?:is\s+)?complete)\b",
        content, re.I,
    ))
    if all(item["status"] == "verified" for item in records) and exhaustive:
        return {"status": "completed", "basis": "All listed requirements are explicitly stated as verified and the source states the checklist is complete.",
                "source_locations": _unique_locations(
                    location for item in records for location in item["source_locations"]
                )}
    return {
        "status": "unable_to_determine",
        "basis": "The extracted statuses do not establish that every required verification item is complete.",
        "source_locations": _unique_locations(
            location for item in records for location in item["source_locations"]
        ),
    }


def _unique_locations(locations):
    unique = []
    for location in locations:
        if location not in unique:
            unique.append(location)
    return unique


def select_verification_records(records: list[dict[str, Any]], view: str) -> list[dict[str, Any]]:
    if view == "verified":
        return [item for item in records if item["status"] == "verified"]
    if view == "pending":
        return [item for item in records if item["status"] == "pending"]
    if view == "action_required":
        return [item for item in records if item["status"] in {"rejected", "failed"}]
    if view == "driving_licence":
        return [item for item in records if re.search(r"driving\s+licen[cs]e", item["document_name"], re.I)]
    if view == "proof_of_address":
        return [item for item in records if re.search(r"proof\s+of\s+address", item["document_name"], re.I)]
    return records


def summarize_verification(records: list[dict[str, Any]], view: str,
                           overall: dict[str, Any]) -> tuple[str, str]:
    """Format a concise answer from explicit extracted statuses only."""
    if not records:
        return "Insufficient evidence: no structured verification checklist was found in Drive records.", "insufficient_evidence"
    if view == "overall":
        overall_value = overall["status"].replace("_", " ").title()
        if overall["status"] == "incomplete":
            return f"Overall status: {overall_value}. {overall['basis']}", "supported"
        if overall["status"] == "action_required":
            return f"Overall status: {overall_value}. {overall['basis']}", "supported"
        return f"Overall status: {overall_value}. {overall['basis']}", "insufficient_evidence"
    selected = select_verification_records(records, view)
    if view in {"driving_licence", "proof_of_address"}:
        label = "Driving licence" if view == "driving_licence" else "Proof of address"
        if not selected:
            return f"{label}: no matching document row was found in the verification checklist.", "insufficient_evidence"
        item = selected[0]
        if item["status"] == "unknown":
            return f"{item['document_name']}: status unavailable; no recognized status label was stated.", "insufficient_evidence"
        return f"{item['document_name']}: {item['status'].title()}.", "supported"
    if view == "verified":
        label = "Documents explicitly marked verified"
    elif view == "pending":
        label = "Documents explicitly marked pending"
    elif view == "action_required":
        label = "Documents explicitly marked rejected or failed"
    else:
        label = "Extracted verification document statuses"
    if not selected:
        return f"{label}: none identified in the extracted checklist rows.", "supported"
    summary = "; ".join(f"{item['document_name']}: {item['status'].title()}" for item in selected)
    if view == "details":
        overall_label = overall["status"].replace("_", " ").title()
        return (f"{label}: {summary}. Overall status: {overall_label}. {overall['basis']}",
                "supported" if all(item["status"] != "unknown" for item in records)
                else "partially_supported")
    return f"{label}: {summary}.", "supported"
