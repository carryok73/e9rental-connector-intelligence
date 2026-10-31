"""Deterministic field extraction for structured rental quotation records."""
from __future__ import annotations

from datetime import date, datetime
import json
import re
from typing import Any


_SAFE_LOCATION_FIELDS = {
    "page", "paragraph", "style", "section", "table", "row", "cell",
    "worksheet", "kind", "record", "line", "line_start", "line_end",
}
_DATE = (
    r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|"
    r"\d{1,2}\s+[A-Za-z]+\s+\d{4}|"
    r"[A-Za-z]+\s+\d{1,2},?\s+\d{4})"
)
_FIELD_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "quotation_number": (
        re.compile(r"\b(?:quotation|quote)\s*(?:number|no\.?|#)\s*[:#-]?\s*(?P<value>[A-Z0-9][A-Z0-9._/-]*)", re.I),
        re.compile(r"\bquote\s*:\s*(?P<value>[A-Z0-9][A-Z0-9._/-]*)", re.I),
    ),
    "quotation_date": (
        re.compile(r"quotation\s+date\s*:\s*(?P<value>[^|\r\n]+)", re.I),
    ),
    "customer_name": (
        re.compile(r"\b(?:prepared\s+for|customer(?:\s+name)?)\s*:\s*(?P<value>[^|\r\n]+)", re.I),
    ),
    "customer_reference": (
        re.compile(r"\bcustomer\s+reference\s*:\s*(?P<value>[^|\r\n]+)", re.I),
    ),
    "vehicle_details": (
        re.compile(r"\bvehicle\s*:\s*(?P<value>[^\r\n]+)", re.I),
    ),
    "estimated_total_amount": (
        re.compile(
            r"\b(?:estimated\s+total|estimated\s+amount|quotation\s+amount|total\s+amount)"
            r"\s*:\s*(?P<value>(?:[A-Z]{3}|[₹$€£])\s*[\d,]+(?:\.\d{1,2})?)",
            re.I,
        ),
    ),
}
_PERIOD_PATTERN = re.compile(
    rf"\brental\s+period\s*:\s*(?P<start>{_DATE})\s+"
    rf"(?:through|to|until|–|—|-)\s+(?P<end>{_DATE})",
    re.I,
)
_DURATION_PATTERNS = (
    re.compile(r"\b(?:rental\s+)?duration\s*:\s*(?P<value>\d+(?:\.\d+)?\s*(?:days?|weeks?|months?))", re.I),
    re.compile(r"\brental\s+period\b[^\r\n]*?\(\s*(?P<value>\d+(?:\.\d+)?\s*(?:days?|weeks?|months?))\s*\)", re.I),
)
_DURATION_VALUE = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>days?|weeks?|months?)", re.I)

FIELD_LABELS = {
    "quotation_number": "Quotation number",
    "quotation_date": "Quotation date",
    "customer_name": "Customer name",
    "customer_reference": "Customer reference",
    "vehicle_details": "Vehicle details",
    "rental_start_date": "Rental start date",
    "rental_end_date": "Rental end date",
    "rental_duration": "Rental duration",
    "estimated_total_amount": "Estimated total amount",
}


def is_rental_quotation_question(question: str) -> bool:
    """Recognize the initial deterministic rental-quotation question family."""
    value = question.casefold()
    if re.search(r"\band\b.*\b(?:invoice|payment\s+status|phone|marketing|budget)\b", value):
        return False
    has_context = bool(re.search(r"\b(?:rental|rent|quotation|quote)\b", value))
    has_intent = bool(re.search(
        r"\b(?:duration|days?|period|start|end|amount|total|price|cost|number|no\.?|"
        r"date|customer|reference|vehicle|details|show|find|list|give|tell)\b",
        value,
    ))
    return has_context and has_intent


def is_rental_quotation_document(content: str) -> bool:
    """Require quotation structure in document content; title and filename do not qualify."""
    # PDF text can concatenate adjacent text blocks (for example
    # "QUOTATIONQuotation date") without a separator; do not require a trailing
    # word boundary after the quotation heading.
    has_rental_quote = bool(re.search(r"\b(?:vehicle\s+)?rental\s+(?:quotation|quote)", content, re.I))
    has_quote_identifier = bool(re.search(
        r"\bquotation\s+date\s*:|\b(?:quotation|quote)\s*(?:number|no\.?|#)\s*[:#-]?|\bquote\s*:",
        content, re.I,
    ))
    has_period = bool(re.search(r"\brental\s+period\s*:", content, re.I))
    return has_rental_quote and has_quote_identifier and has_period


def parse_document_date(value: str) -> date | None:
    """Parse unambiguous ISO or month-name dates; leave ambiguous numeric dates unknown."""
    candidate = value.strip().rstrip(".,")
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%B %d, %Y", "%b %d %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _read_locations(metadata_json: Any, content_length: int) -> list[dict[str, Any]]:
    try:
        metadata = json.loads(metadata_json) if isinstance(metadata_json, str) else {}
    except (TypeError, ValueError):
        return []
    raw = metadata.get("source_locations") if isinstance(metadata, dict) else None
    if not isinstance(raw, list):
        return []
    valid = []
    for item in raw:
        if not isinstance(item, dict) or type(item.get("start")) is not int or type(item.get("end")) is not int:
            continue
        start, end, location = item["start"], item["end"], item.get("location")
        if not (0 <= start <= end <= content_length) or not isinstance(location, dict):
            continue
        safe_location = {
            key: value for key, value in location.items()
            if key in _SAFE_LOCATION_FIELDS and type(value) in {str, int}
        }
        if safe_location:
            valid.append({"start": start, "end": end, "location": safe_location})
    return valid


def _locations_for_span(span_start: int, span_end: int,
                        locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for item in locations:
        start, end = item["start"], item["end"]
        overlaps = start < span_end and end > span_start
        if overlaps and item["location"] not in found:
            found.append(item["location"])
    return found


def _field(content: str, locations: list[dict[str, Any]], key: str,
           match: re.Match[str] | None, *, status: str = "supported",
           value: str | None = None, note: str | None = None) -> dict[str, Any]:
    if match is None:
        return {
            "key": key, "label": FIELD_LABELS[key], "value": None,
            "status": "missing", "evidence": None,
            "start": None, "end": None, "source_locations": [],
            "location_status": "unavailable", "note": note or "No matching field was stated in the document.",
        }
    raw_value = match.groupdict().get("value", "") if value is None else value
    cleaned_value = raw_value.strip() if isinstance(raw_value, str) else raw_value
    group_start, group_end = match.span("value") if "value" in match.re.groupindex else match.span(0)
    trim_left = len(raw_value) - len(raw_value.lstrip()) if isinstance(raw_value, str) else 0
    trim_right = len(raw_value) - len(raw_value.rstrip()) if isinstance(raw_value, str) else 0
    value_start = group_start + trim_left
    value_end = group_end - trim_right
    evidence_start, evidence_end = match.span(0)
    mapped = _locations_for_span(evidence_start, evidence_end, locations)
    return {
        "key": key, "label": FIELD_LABELS[key], "value": cleaned_value,
        "status": status, "evidence": content[evidence_start:evidence_end],
        "start": value_start, "end": value_end,
        "evidence_start": evidence_start, "evidence_end": evidence_end,
        "source_locations": mapped,
        "location_status": "mapped" if mapped else "unavailable",
        "note": note,
    }


def _first_match(content: str, patterns: tuple[re.Pattern[str], ...]) -> re.Match[str] | None:
    return next((match for pattern in patterns if (match := pattern.search(content))), None)


def _duration_number(value: str) -> tuple[float, str] | None:
    match = _DURATION_VALUE.fullmatch(value.strip())
    if not match:
        return None
    return float(match.group("number")), match.group("unit").casefold().rstrip("s")


def extract_rental_quotation(content: str, metadata_json: Any = "{}") -> dict[str, Any]:
    """Extract fields and evidence spans from a structured quotation's content."""
    locations = _read_locations(metadata_json, len(content))
    fields = {
        key: _field(content, locations, key, _first_match(content, patterns))
        for key, patterns in _FIELD_PATTERNS.items()
    }

    period_match = _PERIOD_PATTERN.search(content)
    start_date = end_date = None
    if period_match:
        start_raw = period_match.group("start").strip()
        end_raw = period_match.group("end").strip()
        start_date = parse_document_date(start_raw)
        end_date = parse_document_date(end_raw)
        period_field_text = period_match.group(0)
        period_group_offset = period_match.start(0)
        date_locations = _locations_for_span(*period_match.span(0), locations)
        for key, raw, parsed, span in (
            ("rental_start_date", start_raw, start_date, period_match.span("start")),
            ("rental_end_date", end_raw, end_date, period_match.span("end")),
        ):
            fields[key] = {
                "key": key, "label": FIELD_LABELS[key],
                "value": raw if parsed else None,
                "status": "supported" if parsed else "invalid",
                "evidence": period_field_text,
                "start": span[0], "end": span[1],
                "evidence_start": period_group_offset, "evidence_end": period_match.end(0),
                "source_locations": date_locations,
                "location_status": "mapped" if date_locations else "unavailable",
                "note": None if parsed else "The date text is malformed or ambiguous and was not verified.",
            }
    else:
        for key in ("rental_start_date", "rental_end_date"):
            fields[key] = _field(content, locations, key, None)

    duration_match = _first_match(content, _DURATION_PATTERNS)
    explicit_duration = None
    if duration_match:
        explicit_raw = duration_match.group("value").strip()
        explicit_duration = _duration_number(explicit_raw)
        if explicit_duration is None:
            fields["rental_duration"] = _field(
                content, locations, "rental_duration", duration_match,
                status="invalid", note="The stated duration could not be parsed.",
            )
        else:
            fields["rental_duration"] = _field(content, locations, "rental_duration", duration_match)
            if start_date and end_date:
                elapsed = (end_date - start_date).days
                amount, unit = explicit_duration
                comparable_days = amount if unit == "day" else amount * 7 if unit == "week" else None
                if elapsed < 0 or (comparable_days is not None and comparable_days != elapsed):
                    fields["rental_duration"]["status"] = "conflict"
                    fields["rental_duration"]["note"] = (
                        f"The document states {explicit_raw}; the end date is {abs(elapsed)} elapsed "
                        f"calendar days {'before' if elapsed < 0 else 'after'} the start date. "
                        "The rental-day convention is not stated, so the discrepancy is unresolved."
                    )
                elif unit == "month":
                    fields["rental_duration"]["note"] = (
                        f"The document states {explicit_raw}; calendar-month duration is not compared "
                        "to an elapsed day count."
                    )
    elif start_date and end_date:
        elapsed = (end_date - start_date).days
        if elapsed >= 0:
            fields["rental_duration"] = {
                "key": "rental_duration", "label": FIELD_LABELS["rental_duration"],
                "value": f"{elapsed} elapsed calendar days", "status": "calculated",
                "evidence": period_match.group(0),
                "start": period_match.start(0), "end": period_match.end(0),
                "evidence_start": period_match.start(0), "evidence_end": period_match.end(0),
                "source_locations": _locations_for_span(*period_match.span(0), locations),
                "location_status": "mapped" if _locations_for_span(*period_match.span(0), locations) else "unavailable",
                "note": "Date difference only; the document does not specify inclusive or exclusive rental-day counting.",
            }
        else:
            fields["rental_duration"] = {
                **_field(content, locations, "rental_duration", period_match,
                         status="conflict", value=""),
                "value": None,
                "note": "The end date precedes the start date; duration cannot be calculated.",
            }
    else:
        fields["rental_duration"] = _field(content, locations, "rental_duration", None)

    return {"fields": fields, "source_locations_available": bool(locations)}


def requested_rental_fields(question: str) -> list[str]:
    """Return the structured fields directly requested; default to quotation details."""
    value = question.casefold()
    requested: list[str] = []
    if re.search(r"\b(?:duration|days?)\b", value):
        requested.append("rental_duration")
    if re.search(r"\b(?:amount|total|price|cost)\b", value):
        requested.append("estimated_total_amount")
    if re.search(r"\b(?:start|end|period)\b", value):
        requested.extend(("rental_start_date", "rental_end_date"))
        if "period" in value:
            requested.append("rental_duration")
    if re.search(r"\b(?:number|no\.?|reference)\b", value):
        requested.append("quotation_number" if "customer" not in value else "customer_reference")
    if re.search(r"\bquotation\s+date\b|\bquote\s+date\b", value):
        requested.append("quotation_date")
    if "customer" in value:
        requested.append("customer_name")
        if "reference" in value:
            requested.append("customer_reference")
    if "vehicle" in value and re.search(r"\b(?:what|which|model|type|details)\b", value):
        requested.append("vehicle_details")
    if re.search(r"\b(?:details|all\s+fields)\b", value) or not requested:
        return list(FIELD_LABELS)
    return list(dict.fromkeys(requested))


def summarize_rental_quotation(fields: dict[str, dict[str, Any]], requested: list[str]) -> tuple[str, str]:
    """Build a plain, traceable answer string and aggregate support status."""
    statements = []
    statuses = []
    for key in requested:
        field = fields[key]
        status = field["status"]
        statuses.append(status)
        if status == "supported":
            statements.append(f"{field['label']}: {field['value']}.")
        elif status == "calculated":
            statements.append(
                f"{field['label']}: {field['value']}; {field['note']}"
            )
        elif status == "conflict":
            statements.append(f"{field['label']} discrepancy: {field['note']}")
        elif status == "invalid":
            statements.append(f"{field['label']}: the source text could not be verified as a valid value.")
        else:
            statements.append(f"{field['label']}: not stated in the retrieved quotation.")
    if statuses and all(value in {"supported", "calculated"} for value in statuses):
        overall = "supported"
    elif any(value in {"supported", "calculated"} for value in statuses):
        overall = "partially_supported"
    else:
        overall = "insufficient_evidence"
    return " ".join(statements), overall
