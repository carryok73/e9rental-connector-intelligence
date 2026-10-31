import json
import math
import re
import sqlite3
from datetime import date, datetime
from typing import Any

from .db import count_source_records
from .rental_quote import (
    extract_rental_quotation,
    is_rental_quotation_document,
    is_rental_quotation_question,
    requested_rental_fields,
    summarize_rental_quotation,
)
from .marketing_budget import (
    extract_campaign_budget,
    is_marketing_budget_question,
    requested_budget_view,
    summarize_campaign_budget,
)
from .verification import (
    extract_verification_records,
    is_verification_document,
    is_verification_question,
    overall_verification_status,
    requested_verification_view,
    select_verification_records,
    summarize_verification,
)
from urllib.parse import urlparse


DEFAULT_RESULT_LIMIT = 8
_TOKEN = re.compile(r"[\w]+", re.UNICODE)
_IGNORED_TERMS = {
    "about", "after", "all", "and", "are", "can", "could", "did", "does", "for",
    "from", "have", "how", "into", "is", "it", "many", "me", "of", "on", "our",
    "please", "show", "tell", "that", "the", "their", "them", "there", "these",
    "this", "those", "was", "were", "what", "when", "where", "which", "who", "why",
    "with", "would", "you", "your",
}
_SYNONYM_GROUPS = (
    frozenset({"duration", "period", "days"}),
    frozenset({"amount", "total", "price", "cost"}),
    frozenset({"budget", "expenditure", "allocation"}),
    frozenset({"phone", "telephone", "mobile"}),
)
_SOURCE_LOCATION_FIELDS = {
    "page", "paragraph", "style", "section", "table", "row", "cell",
    "worksheet", "kind", "record", "line", "line_start", "line_end",
}


def _query_terms(question: str) -> list[str]:
    """Return normalized content words; short/common question words do not drive retrieval."""
    return list(dict.fromkeys(
        token for token in _TOKEN.findall(question.casefold())
        if len(token) > 2 and token not in _IGNORED_TERMS
    ))


def _expanded_query_terms(terms: list[str]) -> list[str]:
    """Add explicit retrieval alternatives while retaining each original term first."""
    expanded = list(dict.fromkeys(terms))
    present = set(expanded)
    for group in _SYNONYM_GROUPS:
        if present.intersection(group):
            for term in terms:
                if term in group:
                    for alternative in sorted(group):
                        if alternative not in present:
                            expanded.append(alternative)
                            present.add(alternative)
    return expanded


def _metadata(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_drive_link(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "google.com" or host.endswith(".google.com")):
        return None
    return value


def retrieve(conn: sqlite3.Connection, question: str,
             limit: int | None = DEFAULT_RESULT_LIMIT, *, source: str | None = None,
             selected_group_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Return cross-source FTS results with at least two distinct meaningful query-term matches.

    The overlap threshold is max(2, ceil(half of the normalized query terms)). Ranking
    uses SQLite FTS5 BM25, with source and source ID as stable tie breakers. The limit
    applies after qualification and defaults to eight.
    """
    terms = _query_terms(question)
    expanded_terms = _expanded_query_terms(terms)
    if len(terms) < 2 or (limit is not None and limit <= 0):
        return []
    match = " OR ".join('"' + term.replace('"', '') + '"' for term in expanded_terms)
    filters = ["records_fts MATCH ?"]
    params: list[Any] = [match]
    if source is not None:
        filters.append("r.source=?")
        params.append(source)
    if source == "whatsapp":
        if not selected_group_ids:
            return []
        groups = sorted(selected_group_ids)
        filters.append("r.parent_id IN (" + ",".join("?" for _ in groups) + ")")
        params.extend(groups)
    elif source is None:
        # Generic retrieval never includes WhatsApp unless its current allowlist is supplied.
        if selected_group_ids:
            groups = sorted(selected_group_ids)
            filters.append("(r.source<>'whatsapp' OR r.parent_id IN (" + ",".join("?" for _ in groups) + "))")
            params.extend(groups)
        else:
            filters.append("r.source<>'whatsapp'")
    try:
        rows = conn.execute("""SELECT r.*
          FROM records_fts JOIN records r ON r.id=records_fts.rowid
          WHERE """ + " AND ".join(filters) +
          " ORDER BY bm25(records_fts), r.source, r.source_id", params).fetchall()
    except sqlite3.OperationalError:
        return []

    required = max(2, math.ceil(len(terms) / 2))
    original_term_set = set(terms)
    expanded_term_set = set(expanded_terms)
    qualified = []
    for row in rows:
        record_terms = set(_TOKEN.findall(f"{row['title']} {row['content']}".casefold()))
        original_overlap = len(original_term_set & record_terms)
        expanded_overlap = len(expanded_term_set & record_terms)
        # Alternatives can help bridge one vocabulary mismatch, but a candidate
        # must still contain at least one exact original query term. The original
        # term set remains separate for downstream evidence checks.
        if original_overlap >= required or (original_overlap >= 1 and expanded_overlap >= required):
            qualified.append(dict(row))
    return qualified if limit is None else qualified[:limit]


_PHONE_FIELD_VALUE = re.compile(
    r"\b(?:phone|telephone|mobile)(?:\s+(?:number|no\.?))?\s*[:=|]\s*([^\r\n,;]+)",
    re.I,
)


def _requested_fields(question: str) -> set[str]:
    """Identify a small set of explicit field requests that need field-level evidence."""
    terms = set(_query_terms(question))
    fields: set[str] = set()
    if terms.intersection({"phone", "telephone", "mobile"}):
        fields.add("phone")
    if "duration" in terms or ("rental" in terms and "period" in terms):
        fields.add("duration")
    if terms.intersection({"amount", "total", "price", "cost"}):
        fields.add("amount")
    if terms.intersection({"budget", "expenditure", "allocation"}):
        fields.add("budget")
    if "payment" in terms and terms.intersection({"status", "state"}):
        fields.add("payment_status")
    return fields


def _has_phone_field_value(content: str) -> bool:
    """Require a phone field label and a plausible numeric value on that line."""
    for match in _PHONE_FIELD_VALUE.finditer(content):
        digits = re.sub(r"\D", "", match.group(1))
        if 7 <= len(digits) <= 15:
            return True
    return False


def _supports_requested_fields(row: dict[str, Any], question: str) -> bool:
    """Check requested fields against document content, not title or metadata."""
    fields = _requested_fields(question)
    if not fields:
        return True
    content = str(row.get("content") or "")
    content_terms = set(_TOKEN.findall(content.casefold()))
    if "phone" in fields and not _has_phone_field_value(content):
        return False
    if "duration" in fields:
        has_duration_label = bool(content_terms.intersection({"duration", "period"}))
        has_duration_value = bool(re.search(
            r"\b\d+(?:\.\d+)?\s*(?:days?|weeks?|months?|years?|hours?)\b", content, re.I
        ))
        if not (has_duration_label and has_duration_value):
            return False
    if "amount" in fields:
        has_amount_label = bool(content_terms.intersection(
            {"amount", "total", "price", "cost", "subtotal"}
        ))
        has_numeric_value = bool(re.search(r"\b\d[\d,]*(?:\.\d+)?\b", content))
        if not (has_amount_label and has_numeric_value):
            return False
    if "budget" in fields:
        has_budget_label = bool(content_terms.intersection({"budget", "expenditure", "allocation"}))
        has_numeric_value = bool(re.search(r"\b\d[\d,]*(?:\.\d+)?\b", content))
        if not (has_budget_label and has_numeric_value):
            return False
    if "payment_status" in fields:
        if not re.search(r"\b(?:paid|pending|overdue|unpaid|settled|partially\s+paid)\b", content, re.I):
            return False
    return True


def _split_query_facets(question: str) -> list[str]:
    """Split one simple two-part request when both sides express searchable facets."""
    if '"' in question:
        return []
    parts = re.split(r"\s+\band\b\s+", question, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return []
    cleaned = []
    for part in parts:
        part = re.sub(
            r"^\s*(?:(?:what|which)\s+(?:is|are)|show|find|list|tell\s+me|give\s+me)\s+",
            "", part, flags=re.I,
        )
        part = part.strip(" \t\r\n?.!;:")
        if len(_query_terms(part)) < 2:
            return []
        cleaned.append(part)
    return cleaned


def _citation(row: dict[str, Any], question: str | None = None) -> dict[str, Any]:
    metadata = _metadata(row.get("metadata_json"))
    content = str(row.get("content") or "")
    excerpt = content[:280].strip()
    if question:
        terms = set(_query_terms(question))
        evidence_spans = [
            (match.start(), match.end())
            for match in _TOKEN.finditer(content)
            if match.group(0).casefold() in terms
        ]
    else:
        evidence_spans = [(0, min(280, len(content)))] if excerpt else []
    source_locations = []
    seen_locations: set[str] = set()
    raw_locations = metadata.get("source_locations")
    if isinstance(raw_locations, list):
        for item in raw_locations:
            if not isinstance(item, dict) or type(item.get("start")) is not int or type(item.get("end")) is not int:
                continue
            start, end = item["start"], item["end"]
            location = item.get("location")
            if not (0 <= start <= end <= len(content)) or not isinstance(location, dict):
                continue
            safe_location = {
                key: value for key, value in location.items()
                if key in _SOURCE_LOCATION_FIELDS and type(value) in {str, int}
            }
            if not safe_location:
                continue
            if not any(start < span_end and end > span_start or
                       start == end and span_start <= start <= span_end
                       for span_start, span_end in evidence_spans):
                continue
            marker = json.dumps(safe_location, sort_keys=True, ensure_ascii=False)
            if marker not in seen_locations:
                seen_locations.add(marker)
                source_locations.append(safe_location)
    source = row.get("source")
    parent_id = row.get("parent_id")
    citation = {
        "source": source,
        "source_type": source,
        "source_id": row.get("source_id"),
        "title": row.get("title"),
        "occurred_at": row.get("occurred_at"),
        "timestamp": row.get("occurred_at"),
        "parent_id": parent_id,
        "sender": metadata.get("sender") if isinstance(metadata.get("sender"), str) else None,
        "group_context": parent_id if source == "whatsapp" else None,
        "thread_id": parent_id if source == "gmail" else None,
        "drive_link": _safe_drive_link(metadata.get("web_view_link")) if source == "drive" else None,
        "excerpt": excerpt or row.get("title", ""),
        "source_locations": source_locations,
    }
    return citation


def _facet_search_result(conn: sqlite3.Connection, facets: list[str],
                         selected_groups: set[str] | None) -> dict[str, Any]:
    """Retrieve, validate, and combine evidence independently for each facet."""
    facet_results: list[dict[str, Any]] = []
    combined_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for facet in facets:
        candidates = retrieve(conn, facet, limit=DEFAULT_RESULT_LIMIT,
                              selected_group_ids=selected_groups)
        supported = [row for row in candidates if _supports_requested_fields(row, facet)]
        facet_citations = []
        for candidate in supported:
            row = dict(candidate)
            row["match_type"] = "topic"
            key = (str(row.get("source", "")), str(row.get("source_id", "")))
            if key not in combined_by_key:
                row["_citation_locations"] = []
                combined_by_key[key] = row
            citation = _citation(row, facet)
            citation["match_type"] = "topic"
            facet_citations.append(citation)
            existing_locations = combined_by_key[key]["_citation_locations"]
            for location in citation["source_locations"]:
                if location not in existing_locations:
                    existing_locations.append(location)
        facet_results.append({
            "facet": facet,
            "status": "supported" if supported else "insufficient_evidence",
            "candidate_count": len(candidates),
            "supporting_record_count": len(supported),
            "citations": facet_citations,
        })

    evidence = list(combined_by_key.values())
    citations = []
    by_source = {source: {"exact_literal": 0, "topic": 0, "total": 0}
                 for source in ("whatsapp", "gmail", "drive")}
    for row in evidence:
        citation = _citation(row)
        citation["source_locations"] = row.pop("_citation_locations", citation["source_locations"])
        citation["match_type"] = "topic"
        citations.append(citation)
        counts = by_source[row["source"]]
        counts["topic"] += 1
        counts["total"] += 1

    supported_facets = sum(item["status"] == "supported" for item in facet_results)
    if supported_facets == len(facet_results):
        response_text = (
            f"Supporting evidence was retrieved for all {len(facet_results)} requested facets. "
            "This is evidence retrieval only; it does not generate a factual answer or establish "
            "that separate records concern the same customer or transaction."
        )
    elif supported_facets:
        missing = "; ".join(item["facet"] for item in facet_results
                             if item["status"] != "supported")
        response_text = (
            f"Supporting evidence was retrieved for {supported_facets} of {len(facet_results)} "
            f"requested facets. Insufficient evidence for: {missing}. Retrieved records are "
            "evidence only; no factual answer was generated."
        )
    else:
        missing = "; ".join(item["facet"] for item in facet_results)
        response_text = f"Insufficient evidence for all requested facets: {missing}. No factual answer was generated."

    return {
        "answer": response_text,
        "citations": citations,
        "evidence": evidence,
        "matching_counts": by_source,
        "total_matching_records": len(evidence),
        "facets": facet_results,
        "whatsapp_scope": {
            "status": "filtered_to_selected_groups" if selected_groups else "excluded_no_groups_selected",
            "selected_group_count": len(selected_groups or set()),
            "note": "WhatsApp records are filtered by the current selected-group allowlist.",
        },
    }


def _exact_literal_matches(conn: sqlite3.Connection, phrase: str,
                           selected_groups: set[str] | None) -> list[dict[str, Any]]:
    """Match a quoted literal as a case-sensitive, punctuation-sensitive substring."""
    if len(phrase) < 3 or phrase.casefold() in _IGNORED_TERMS:
        return []
    filters = ["(instr(title, ?) > 0 OR instr(content, ?) > 0)"]
    params: list[Any] = [phrase, phrase]
    if selected_groups:
        groups = sorted(selected_groups)
        filters.append("(source<>'whatsapp' OR parent_id IN (" + ",".join("?" for _ in groups) + "))")
        params.extend(groups)
    else:
        filters.append("source<>'whatsapp'")
    rows = conn.execute("SELECT * FROM records WHERE " + " AND ".join(filters) +
                        " ORDER BY source, source_id", params).fetchall()
    return [dict(row) for row in rows]


def _search_result(conn: sqlite3.Connection, question: str,
                   selected_groups: set[str] | None,
                   *, count_intent: bool = False) -> dict[str, Any]:
    literals = re.findall(r'"([^"]+)"', question)
    if question.count('"') % 2 or len(literals) > 1:
        return {"answer": "Unsupported search: provide at most one complete double-quoted literal phrase.",
                "citations": [], "evidence": [], "matching_counts": {}, "total_matching_records": 0,
                "whatsapp_scope": {
                    "status": "filtered_to_selected_groups" if selected_groups else "excluded_no_groups_selected",
                    "selected_group_count": len(selected_groups or set()),
                    "note": "WhatsApp records are filtered by the current selected-group allowlist.",
                }}
    literal = literals[0] if len(literals) == 1 else None
    facets = [] if literal or count_intent else _split_query_facets(question)
    if facets:
        return _facet_search_result(conn, facets, selected_groups)
    exact_rows = _exact_literal_matches(conn, literal, selected_groups) if literal else []

    # A quoted phrase alone is an exact search. Topic retrieval is combined only
    # when the user separately introduces a topic with "about".
    topic_query = question
    if literal:
        about = re.search(r"\babout\s+(.+?)(?=\s+(?:containing|with exact literal)\b|[?.!]|$)", question, re.I)
        topic_query = about.group(1).replace(f'"{literal}"', "").strip() if about else ""
    topic_rows = retrieve(conn, topic_query, limit=None, selected_group_ids=selected_groups) if topic_query else []
    exact_rows = [row for row in exact_rows if _supports_requested_fields(row, question)]
    topic_rows = [row for row in topic_rows if _supports_requested_fields(row, question)]
    exact_keys = {(row["source"], row["source_id"]) for row in exact_rows}
    topic_rows = [row for row in topic_rows if (row["source"], row["source_id"]) not in exact_keys]

    by_source = {source: {"exact_literal": 0, "topic": 0, "total": 0}
                 for source in ("whatsapp", "gmail", "drive")}
    for row in exact_rows:
        by_source[row["source"]]["exact_literal"] += 1
        by_source[row["source"]]["total"] += 1
    for row in topic_rows:
        by_source[row["source"]]["topic"] += 1
        by_source[row["source"]]["total"] += 1

    combined = []
    for row in exact_rows:
        row["match_type"] = "exact_literal"
        combined.append(row)
    for row in topic_rows:
        row["match_type"] = "topic"
        combined.append(row)
    displayed = combined[:DEFAULT_RESULT_LIMIT]
    citations = []
    for row in displayed:
        citation = _citation(row, question)
        citation["match_type"] = row["match_type"]
        citations.append(citation)
    total = sum(item["total"] for item in by_source.values())
    if count_intent:
        counts = ", ".join(f"{source.title()}: {data['total']}" for source, data in by_source.items())
        response_text = (f"Found {total} matching stored record(s) by source: {counts}. "
                         "These are record matches, not customer, order, or transaction counts.")
    elif citations:
        response_text = (f"Found {total} matching stored record(s). Exact literal text matches and qualified "
                         "topic matches are labeled separately. Shared text does not establish a verified "
                         "customer, order, or transaction relationship. This is evidence retrieval, not a verified analytical answer.")
    else:
        response_text = "Insufficient evidence in the collected records to answer this question."
    return {
        "answer": response_text,
        "citations": citations,
        "evidence": displayed,
        "matching_counts": by_source,
        "total_matching_records": total,
        "facets": [{
            "facet": question.strip(),
            "status": "supported" if citations else "insufficient_evidence",
            "candidate_count": len(exact_rows) + len(topic_rows),
            "supporting_record_count": len(displayed),
            "citations": citations,
        }],
        "whatsapp_scope": {
            "status": "filtered_to_selected_groups" if selected_groups else "excluded_no_groups_selected",
            "selected_group_count": len(selected_groups or set()),
            "note": ("WhatsApp results are limited to stored records admitted under the current configured allowlist; "
                     "organizer group-ID mapping and authorization remain unverified."),
        },
    }


def _unanswered_email_analytics(conn: sqlite3.Connection, list_mode: bool) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM records WHERE source='gmail' ORDER BY occurred_at, source_id"
    ).fetchall()
    criteria = (
        "Gmail records whose stored is_inbound field is true and stored is_unanswered field is true; "
        "the connector computes unanswered status using its inclusive UTC calendar-date cutoff."
    )
    if not rows:
        return {
            "answer": "Unavailable: there are no stored Gmail records. Sync the authorized mailbox before asking for this count.",
            "citations": [], "evidence": [],
            "analytics": {"status": "unavailable", "count": None, "criteria": criteria,
                          "coverage": "Only stored Gmail records are considered; mailbox sync completeness is not tracked."},
        }

    parsed_rows: list[dict[str, Any]] = []
    problems = 0
    policy: set[tuple[str, int]] = set()
    for raw in rows:
        row = dict(raw)
        metadata = _metadata(row.get("metadata_json"))
        reference = metadata.get("unanswered_reference_date")
        days = metadata.get("unanswered_days")
        sender = metadata.get("sender")
        occurred_at = row.get("occurred_at")
        valid = (
            type(metadata.get("is_inbound")) is bool
            and type(metadata.get("is_unanswered")) is bool
            and isinstance(sender, str) and bool(sender.strip())
            and isinstance(reference, str)
            and type(days) is int and days >= 0
            and isinstance(occurred_at, str) and bool(occurred_at)
        )
        try:
            if not valid:
                raise ValueError
            ref_day = date.fromisoformat(reference)
            occurred_day = datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).date()
        except (TypeError, ValueError, OverflowError):
            problems += 1
            continue
        policy.add((ref_day.isoformat(), days))
        row["_analytics_metadata"] = metadata
        parsed_rows.append(row)

    if len(policy) > 1:
        problems += 1
    if problems or len(parsed_rows) != len(rows):
        return {
            "answer": (f"Unavailable: unanswered-email analytics require valid inbound, unanswered, sender, "
                       f"timestamp, reference-date, and cutoff metadata for every stored Gmail record. "
                       f"{problems} record or policy issue(s) need review."),
            "citations": [], "evidence": [],
            "analytics": {"status": "incomplete", "count": None, "criteria": criteria,
                          "stored_gmail_records": len(rows), "valid_records": len(parsed_rows),
                          "coverage": "Only stored Gmail records are considered; mailbox sync completeness is not tracked."},
        }

    reference_date, days = next(iter(policy))
    criteria = (
        f"Gmail records with is_inbound=true and is_unanswered=true, as computed by the connector "
        f"for reference date {reference_date} and an inclusive {days}-day UTC calendar-date cutoff "
        f"(message date <= reference date minus {days} days)."
    )
    matches = [row for row in parsed_rows
               if row["_analytics_metadata"]["is_inbound"] is True
               and row["_analytics_metadata"]["is_unanswered"] is True]
    for row in parsed_rows:
        row.pop("_analytics_metadata", None)
    count = len(matches)
    coverage = "Count is over stored Gmail records only; mailbox sync completeness is not tracked."
    if list_mode:
        citations = [_citation(row) for row in matches]
        answer_text = (f"Found {count} unanswered inbound email(s) among stored Gmail records. "
                       f"{criteria} {coverage}")
        return {
            "answer": answer_text, "citations": citations, "evidence": matches,
            "analytics": {"status": "complete_for_stored_records", "count": count,
                          "criteria": criteria, "coverage": coverage},
        }
    return {
        "answer": f"There are {count} unanswered inbound email(s) among stored Gmail records. {criteria} {coverage}",
        "citations": [], "evidence": [],
        "analytics": {"status": "complete_for_stored_records", "count": count,
                      "criteria": criteria, "coverage": coverage},
    }


def _requested_whatsapp_group(question: str) -> tuple[bool, str | None]:
    """Parse the narrow literal syntax 'group <ID>'; group names are not resolved."""
    match = re.search(r"\bgroup\s+(.+?)(?=\s+about\b|[?!]|$)", question, re.I)
    if not match:
        return False, None
    requested = match.group(1).strip().strip("\"'").strip().rstrip(".").strip()
    return True, requested or None


def _candidate_strings(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _whatsapp_candidates(conn: sqlite3.Connection, *, key: str, label: str,
                         selected_groups: set[str], question: str,
                         topic: str | None = None, require_decision: bool = False) -> dict[str, Any]:
    if not selected_groups:
        return {"answer": "Unavailable: no WhatsApp groups are currently selected in configuration.",
                "citations": [], "evidence": []}

    has_group, requested_group = _requested_whatsapp_group(question)
    if has_group:
        if requested_group not in selected_groups:
            return {"answer": "No results: the requested group ID is not an exact member of the configured selected-group allowlist.",
                    "citations": [], "evidence": []}
        group_ids = {requested_group}
    else:
        group_ids = selected_groups

    rows: list[dict[str, Any]]
    if topic:
        rows = retrieve(conn, topic, source="whatsapp", selected_group_ids=group_ids)
    else:
        placeholders = ",".join("?" for _ in group_ids)
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM records WHERE source='whatsapp' AND parent_id IN (" + placeholders + ") "
            "ORDER BY occurred_at, source_id", sorted(group_ids)
        ).fetchall()]

    matches: list[dict[str, Any]] = []
    malformed = 0
    for row in rows:
        if topic and not require_decision:
            matches.append(row)
            continue
        metadata = _metadata(row.get("metadata_json"))
        interpretation = metadata.get("interpretation")
        if not isinstance(interpretation, dict):
            malformed += 1
            continue
        values = _candidate_strings(interpretation.get(key))
        if values is None:
            malformed += 1
            continue
        if not values:
            continue
        if require_decision:
            decisions = _candidate_strings(interpretation.get("decisions"))
            if not decisions:
                continue
        row["candidate_type"] = label
        row["candidate_text"] = values
        matches.append(row)

    citations = []
    for row in matches:
        citation = _citation(row)
        if label != "topic matches":
            citation["candidate_type"] = label
            citation["interpretation_label"] = "Heuristic candidate; requires human review"
        citations.append(citation)
    group_scope = (f"Group {next(iter(group_ids))}" if has_group
                   else f"configured group IDs: {', '.join(sorted(group_ids))}")
    coverage = (f"Results are limited to stored WhatsApp records admitted under the current configured allowlist "
                f"({group_scope}); organizer group-ID mapping and authorization remain unverified.")
    if topic and not require_decision:
        description = "WhatsApp messages matching the topic"
    elif topic:
        description = "WhatsApp messages matching the topic and containing decision candidates"
    else:
        description = f"WhatsApp {label.lower()}"
    answer_text = f"Found {len(matches)} {description} in stored records. "
    if label != "topic matches":
        answer_text += "Interpretations are heuristic candidates, not confirmed decisions or commitments. "
    if require_decision:
        answer_text += "These records are matching candidate evidence, not verified proof. "
    answer_text += coverage
    if malformed:
        answer_text += f" Skipped {malformed} record(s) with missing or malformed interpretation metadata."
    return {"answer": answer_text, "citations": citations, "evidence": matches,
            "candidate_label": label, "coverage": coverage}


def _whatsapp_query(conn: sqlite3.Connection, question: str,
                    selected_groups: set[str]) -> dict[str, Any] | None:
    q = question.casefold()
    if "whatsapp" not in q or not re.search(r"\b(show|list|find)\b", q):
        return None
    if re.search(r"\bevidence\b", q) and re.search(r"\bdecision\b", q):
        about = re.search(r"\babout\s+(.+?)(?:\s+\bgroup\b|[?.!]|$)", question, re.I)
        if not about:
            return {"answer": "Unsupported query: candidate decision evidence requires a topic after 'about'.",
                    "citations": [], "evidence": []}
        topic = about.group(1).strip()
        return _whatsapp_candidates(conn, key="decisions", label="potential decision candidates",
                                    selected_groups=selected_groups, question=question,
                                    topic=topic, require_decision=True)
    if re.search(r"\bpotential\s+decisions?\b", q):
        return _whatsapp_candidates(conn, key="decisions", label="potential decision candidates",
                                    selected_groups=selected_groups, question=question)
    if re.search(r"\bpotential\s+commitments?\b", q):
        return _whatsapp_candidates(conn, key="requests_and_commitments",
                                    label="request/commitment candidates",
                                    selected_groups=selected_groups, question=question)
    if re.search(r"\bcandidate\s+action\s+items?\b", q):
        return _whatsapp_candidates(conn, key="action_items", label="candidate action items",
                                    selected_groups=selected_groups, question=question)
    topic_match = re.search(r"\babout\s+(.+?)(?:\s+\bgroup\b|[?.!]|$)", question, re.I)
    if topic_match and re.search(r"\bmessages?\b", q):
        return _whatsapp_candidates(conn, key="", label="topic matches",
                                    selected_groups=selected_groups, question=question,
                                    topic=topic_match.group(1).strip())
    return None


def _rental_quotation_result(conn: sqlite3.Connection, question: str,
                             selected_groups: set[str]) -> dict[str, Any]:
    """Answer a supported quotation question from structured Drive quotation content."""
    requested = requested_rental_fields(question)
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for locator_query in ("vehicle rental", "rental period", "quotation date"):
        for row in retrieve(conn, locator_query, limit=None, source="drive"):
            candidates_by_id.setdefault(str(row.get("source_id", "")), row)
    candidates = list(candidates_by_id.values())
    quotations = [row for row in candidates
                  if is_rental_quotation_document(str(row.get("content") or ""))]
    if not quotations:
        # A field-specific rental request with Drive candidates but no
        # quotation structure must not be answered from incidental mentions.
        if (candidates or "rental_duration" in requested) and re.search(
            r"\b(?:duration|days?|start|end|period|amount|total|price|cost|number|no\.?)\b",
            question, re.I,
        ):
            facet = {"facet": question.strip(), "status": "insufficient_evidence",
                     "candidate_count": len(candidates), "supporting_record_count": 0,
                     "citations": []}
            return {
                "answer": "Insufficient evidence: retrieved Drive records do not contain a structured rental quotation.",
                "citations": [], "evidence": [],
                "matching_counts": {source: {"exact_literal": 0, "topic": 0, "total": 0}
                                    for source in ("whatsapp", "gmail", "drive")},
                "total_matching_records": 0, "facets": [facet],
                "rental_answer": {"status": "insufficient_evidence", "requested_fields": requested,
                                  "documents": []},
            }
        # Preserve the established topic retrieval path when the database has
        # no structured quotation record. The deterministic answerer only
        # takes ownership once it has a content-verified quotation to parse.
        return _search_result(conn, question, selected_groups)

    if not quotations:
        facet = {"facet": question.strip(), "status": "insufficient_evidence",
                 "candidate_count": len(candidates), "supporting_record_count": 0, "citations": []}
        return {
            "answer": "Insufficient evidence: no structured rental quotation was retrieved from Drive records.",
            "citations": [], "evidence": [],
            "matching_counts": {source: {"exact_literal": 0, "topic": 0, "total": 0}
                                for source in ("whatsapp", "gmail", "drive")},
            "total_matching_records": 0,
            "facets": [facet],
            "rental_answer": {"status": "insufficient_evidence", "requested_fields": requested,
                              "documents": []},
            "whatsapp_scope": {
                "status": "filtered_to_selected_groups" if selected_groups else "excluded_no_groups_selected",
                "selected_group_count": len(selected_groups),
                "note": "WhatsApp records are filtered by the current selected-group allowlist.",
            },
        }

    documents = []
    citations = []
    summaries = []
    by_source = {source: {"exact_literal": 0, "topic": 0, "total": 0}
                 for source in ("whatsapp", "gmail", "drive")}
    for row in quotations:
        extracted = extract_rental_quotation(str(row.get("content") or ""), row.get("metadata_json"))
        fields = extracted["fields"]
        summary, status = summarize_rental_quotation(fields, requested)
        title = row.get("title") or "(untitled quotation)"
        documents.append({
            "source_id": row.get("source_id"), "title": title,
            "status": status, "fields": list(fields.values()),
            "requested_fields": requested,
        })
        summaries.append(f"{title}: {summary}")

        citation = _citation(row, "rental quotation")
        relevant_locations = []
        relevant_evidence = []
        for key in requested:
            field = fields[key]
            if field.get("status") in {"supported", "calculated"}:
                relevant_evidence.append(field.get("evidence"))
                for location in field.get("source_locations", []):
                    if location not in relevant_locations:
                        relevant_locations.append(location)
        citation["source_locations"] = relevant_locations
        citation["excerpt"] = next((item for item in relevant_evidence if item), citation["excerpt"])
        citation["match_type"] = "topic"
        citations.append(citation)
        by_source["drive"]["topic"] += 1
        by_source["drive"]["total"] += 1

    statuses = [document["status"] for document in documents]
    overall = ("supported" if all(status == "supported" for status in statuses) else
               "insufficient_evidence" if all(status == "insufficient_evidence" for status in statuses)
               else "partially_supported")
    answer_text = " ".join(summaries)
    if overall != "supported":
        answer_text = f"Evidence status: {overall.replace('_', ' ')}. " + answer_text
    facet = {"facet": question.strip(), "status": "supported" if overall == "supported" else "insufficient_evidence",
             "candidate_count": len(candidates), "supporting_record_count": len(quotations),
             "citations": citations}
    return {
        "answer": answer_text,
        "citations": citations,
        "evidence": quotations,
        "matching_counts": by_source,
        "total_matching_records": len(quotations),
        "facets": [facet],
        "rental_answer": {"status": overall, "requested_fields": requested,
                          "documents": documents},
        "whatsapp_scope": {
            "status": "filtered_to_selected_groups" if selected_groups else "excluded_no_groups_selected",
            "selected_group_count": len(selected_groups),
            "note": "WhatsApp records are filtered by the current selected-group allowlist.",
        },
    }


def _marketing_budget_result(conn: sqlite3.Connection, question: str,
                             selected_groups: set[str]) -> dict[str, Any]:
    """Answer campaign expenditure questions from structured Drive workbook rows."""
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for locator_query in ("campaign budget", "planned expenditure", "actual expenditure"):
        for row in retrieve(conn, locator_query, limit=None, source="drive"):
            candidates_by_id.setdefault(str(row.get("source_id", "")), row)
    view = requested_budget_view(question)
    documents = []
    citations = []
    all_campaigns = []
    missing_structured_values = bool(candidates_by_id)

    for row in candidates_by_id.values():
        content = str(row.get("content") or "")
        extracted = extract_campaign_budget(content, row.get("metadata_json"))
        campaigns = extracted["campaigns"]
        if not campaigns:
            continue
        missing_structured_values = False
        summary, status, calculations = summarize_campaign_budget(campaigns, view)
        title = row.get("title") or "(untitled campaign workbook)"
        document = {
            "source_id": row.get("source_id"), "title": title,
            "status": status, "view": view, "campaigns": campaigns,
            "summary": summary, "calculations": calculations,
        }
        documents.append(document)
        all_campaigns.extend(campaigns)

        locations = []
        for campaign in campaigns:
            for location in campaign["source_locations"]:
                if location not in locations:
                    locations.append(location)
        evidence_lines = list(dict.fromkeys(campaign["evidence"] for campaign in campaigns))
        citation = _citation(row, "campaign budget")
        citation["source_locations"] = locations
        citation["excerpt"] = "\n".join(evidence_lines)
        citation["match_type"] = "topic"
        citations.append(citation)

    if not documents:
        # Keep the generic evidence-search contract for spreadsheets that do
        # not have the structured campaign columns required for calculations.
        return _search_result(conn, question, selected_groups)
    else:
        status = ("supported" if all(item["status"] == "supported" for item in documents)
                  else "partially_supported")
        answer_text = " ".join(f"{document['title']}: {document['summary']}" for document in documents)

    by_source = {source: {"exact_literal": 0, "topic": 0, "total": 0}
                 for source in ("whatsapp", "gmail", "drive")}
    by_source["drive"]["topic"] = len(documents)
    by_source["drive"]["total"] = len(documents)
    facet = {"facet": question.strip(), "status": status,
             "candidate_count": len(candidates_by_id), "supporting_record_count": len(documents),
             "citations": citations}
    return {
        "answer": answer_text, "citations": citations,
        "evidence": [row for row in candidates_by_id.values()
                     if any(row.get("source_id") == item["source_id"] for item in documents)],
        "matching_counts": by_source, "total_matching_records": len(documents),
        "facets": [facet],
        "marketing_answer": {
            "status": status, "view": view, "documents": documents,
            "candidate_count": len(candidates_by_id),
            "note": ("No structured campaign budget rows were verified."
                     if missing_structured_values and not documents else None),
        },
    }


def _verification_result(conn: sqlite3.Connection, question: str,
                         selected_groups: set[str]) -> dict[str, Any]:
    """Answer verification checklist questions from structured Drive document text."""
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for locator_query in ("customer verification", "verification status", "kyc checklist"):
        for row in retrieve(conn, locator_query, limit=None, source="drive"):
            candidates_by_id.setdefault(str(row.get("source_id", "")), row)
    view = requested_verification_view(question)
    documents = []
    citations = []
    summaries = []

    for row in candidates_by_id.values():
        content = str(row.get("content") or "")
        if not is_verification_document(content):
            continue
        extracted = extract_verification_records(content, row.get("metadata_json"))
        records = extracted["records"]
        overall = overall_verification_status(records, content)
        summary, status = summarize_verification(records, view, overall)
        source_note = extracted.get("source_note")
        if source_note:
            summary += " " + source_note["text"]
        title = row.get("title") or "(untitled verification record)"
        documents.append({
            "source_id": row.get("source_id"), "title": title,
            "status": status, "view": view, "records": records,
            "overall": overall,
            "evidence_coverage": (
                "Checklist completeness is explicitly established."
                if overall["status"] == "completed"
                else "The source does not establish that the checklist covers every requirement."
            ),
            "source_note": source_note,
        })
        summaries.append(f"{title}: {summary}")

        selected = (select_verification_records(records, view)
                    if view != "overall" else records)
        evidence = [item["evidence"] for item in selected]
        locations = []
        for item in selected:
            for location in item["source_locations"]:
                if location not in locations:
                    locations.append(location)
        if source_note:
            evidence.append(source_note["evidence"])
            for location in source_note["source_locations"]:
                if location not in locations:
                    locations.append(location)
        citation = _citation(row, "verification checklist")
        citation["source_locations"] = locations
        citation["excerpt"] = "\n".join(evidence) if evidence else content[:280].strip()
        citation["match_type"] = "topic"
        citations.append(citation)

    if not documents:
        return _search_result(conn, question, selected_groups)
    statuses = [document["status"] for document in documents]
    answer_status = ("supported" if all(value == "supported" for value in statuses)
                     else "partially_supported" if any(value == "supported" for value in statuses)
                     else "insufficient_evidence")
    answer_text = " ".join(summaries)
    by_source = {source: {"exact_literal": 0, "topic": 0, "total": 0}
                 for source in ("whatsapp", "gmail", "drive")}
    by_source["drive"]["topic"] = len(documents)
    by_source["drive"]["total"] = len(documents)
    facet = {"facet": question.strip(), "status": answer_status,
             "candidate_count": len(candidates_by_id), "supporting_record_count": len(documents),
             "citations": citations}
    return {
        "answer": answer_text, "citations": citations,
        "evidence": [row for row in candidates_by_id.values()
                     if any(row.get("source_id") == item["source_id"] for item in documents)],
        "matching_counts": by_source, "total_matching_records": len(documents),
        "facets": [facet],
        "verification_answer": {"status": answer_status, "view": view,
                                 "documents": documents,
                                 "candidate_count": len(candidates_by_id)},
    }


def answer(conn: sqlite3.Connection, question: str,
           selected_whatsapp_groups: set[str] | None = None) -> dict[str, Any]:
    q = question.lower()
    selected_groups = selected_whatsapp_groups or set()
    whatsapp_result = _whatsapp_query(conn, question, selected_groups)
    if whatsapp_result is not None:
        return whatsapp_result
    if is_verification_question(question):
        return _verification_result(conn, question, selected_groups)
    if is_marketing_budget_question(question):
        return _marketing_budget_result(conn, question, selected_groups)
    if is_rental_quotation_question(question):
        return _rental_quotation_result(conn, question, selected_groups)
    if "unanswered" in q:
        is_email_query = bool(re.search(r"\b(?:e-?mails?|gmail)\b", q))
        count_intent = bool(re.search(r"\b(?:how many|count|number of)\b", q))
        list_intent = bool(re.search(r"\b(?:show|list|find|which)\b", q))
        if is_email_query and (count_intent or list_intent):
            return _unanswered_email_analytics(conn, list_mode=list_intent and not count_intent)
        if count_intent or list_intent:
            return {
                "answer": "Unsupported analytical question: unanswered status is currently defined only for stored Gmail email records.",
                "citations": [], "evidence": [],
            }
    matching_count_intent = (
        bool(re.search(r"\b(?:how many|count|number of)\b", q))
        and bool(re.search(r"\b(?:records?|messages?|documents?)\b", q))
        and bool(re.search(r"\b(?:match|matching|mention|mentions|contain|contains)\b", q))
    )
    if matching_count_intent:
        return _search_result(conn, question, selected_groups, count_intent=True)
    if re.search(r"\b(how many|count|number of)\b", q):
        source = next((s for s in ("whatsapp", "gmail", "drive") if s in q), None)
        if source:
            count = count_source_records(conn, source, selected_groups)
            result = {"answer": f"There are {count} {source} records in the database.",
                      "citations": [], "evidence": []}
            if source == "whatsapp":
                result["whatsapp_scope"] = {
                    "status": "filtered_to_selected_groups" if selected_groups else "excluded_no_groups_selected",
                    "selected_group_count": len(selected_groups),
                    "note": "Count includes only stored WhatsApp records under the current allowlist; organizer mapping remains unverified.",
                }
            return result
    return _search_result(conn, question, selected_groups)
