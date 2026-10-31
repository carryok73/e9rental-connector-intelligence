"""Small, defensive formatting helpers for citation details in the dashboard."""
from __future__ import annotations

from typing import Any


_LOCATION_FIELDS = {
    "page", "paragraph", "style", "section", "table", "row", "cell",
    "worksheet", "kind", "record", "line", "line_start", "line_end",
}


def format_source_location(location: Any) -> str | None:
    """Format known extracted location shapes without displaying arbitrary metadata."""
    if not isinstance(location, dict):
        return None
    safe = {
        key: value for key, value in location.items()
        if key in _LOCATION_FIELDS and type(value) in {str, int}
    }
    for key in ("page", "paragraph", "table", "row", "cell", "record", "line",
                "line_start", "line_end"):
        if key in safe and (type(safe[key]) is not int or safe[key] < 1):
            return None
    if "line_start" in safe or "line_end" in safe:
        if "line_start" not in safe or "line_end" not in safe or safe["line_end"] < safe["line_start"]:
            return None
    for key in ("worksheet", "section", "style"):
        if key in safe and not safe[key].strip():
            return None
    if "page" in safe:
        return f"PDF · page {safe['page']}"
    if "worksheet" in safe:
        sheet = safe["worksheet"]
        if safe.get("kind") == "worksheet_header":
            return f"XLSX · worksheet {sheet} (header)"
        if "row" in safe:
            return f"XLSX · worksheet {sheet}, row {safe['row']}"
        return f"XLSX · worksheet {sheet}"
    if "table" in safe:
        detail = f"DOCX · table {safe['table']}"
        if "row" in safe:
            detail += f", row {safe['row']}"
        if "cell" in safe:
            detail += f", cell {safe['cell']}"
        return detail
    if "paragraph" in safe:
        detail = f"DOCX · paragraph {safe['paragraph']}"
        if safe.get("section"):
            detail += f" · {safe['section']}"
        return detail
    if "record" in safe:
        detail = f"CSV · record {safe['record']}"
        line_start = safe.get("line_start")
        line_end = safe.get("line_end")
        if line_start is not None and line_end is not None:
            detail += (f", line {line_start}" if line_start == line_end
                       else f", lines {line_start}–{line_end}")
        return detail
    if "line" in safe:
        return f"Line {safe['line']}"
    if "line_start" in safe and "line_end" in safe:
        return (f"Lines {safe['line_start']}–{safe['line_end']}"
                if safe["line_start"] != safe["line_end"]
                else f"Line {safe['line_start']}")
    return None


def prepare_citation_display(citation: Any) -> dict[str, Any] | None:
    """Return only concise, display-safe fields for one citation."""
    if not isinstance(citation, dict):
        return None
    source = citation.get("source_type") or citation.get("source")
    title = citation.get("title")
    excerpt = citation.get("excerpt")
    locations = citation.get("source_locations")
    formatted_locations = []
    seen = set()
    if isinstance(locations, list):
        for location in locations:
            label = format_source_location(location)
            if label and label not in seen:
                formatted_locations.append(label)
                seen.add(label)
    return {
        "source": citation.get("source") if isinstance(citation.get("source"), str) else source,
        "source_type": source.title() if isinstance(source, str) and source else "Source",
        "source_id": citation.get("source_id") if isinstance(citation.get("source_id"), str) else "",
        "title": title if isinstance(title, str) and title else "(untitled record)",
        "excerpt": excerpt if isinstance(excerpt, str) and excerpt else "No excerpt is available.",
        "locations": formatted_locations,
        "drive_link": citation.get("drive_link") if isinstance(citation.get("drive_link"), str) else None,
        "timestamp": citation.get("timestamp") if isinstance(citation.get("timestamp"), str) else None,
        "sender": citation.get("sender") if isinstance(citation.get("sender"), str) else None,
        "thread_id": citation.get("thread_id") if isinstance(citation.get("thread_id"), str) else None,
        "group_context": citation.get("group_context") if isinstance(citation.get("group_context"), str) else None,
        "parent_id": citation.get("parent_id") if isinstance(citation.get("parent_id"), str) else None,
        "interpretation_label": (citation.get("interpretation_label")
                                  if isinstance(citation.get("interpretation_label"), str) else None),
    }


def prepare_facet_display(facets: Any) -> list[dict[str, Any]]:
    """Normalize multi-facet results while keeping unsupported facets explicit."""
    if not isinstance(facets, list):
        return []
    prepared = []
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        citations = facet.get("citations")
        raw_citations = citations if isinstance(citations, list) else []
        prepared.append({
            "facet": facet.get("facet") if isinstance(facet.get("facet"), str) else "Requested information",
            "status": "Supported" if facet.get("status") == "supported" else "Insufficient evidence",
            "citations": [value for value in
                          (prepare_citation_display(item) for item in raw_citations)
                          if value is not None],
        })
    return prepared
