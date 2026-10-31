from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from src.connector_intelligence import config
from src.connector_intelligence.citation_display import (
    format_source_location,
    prepare_citation_display,
    prepare_facet_display,
)
from src.connector_intelligence.db import connect, upsert_record


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app.py"


def _visible_strings(element):
    for attribute in ("value", "label"):
        value = getattr(element, attribute, None)
        if isinstance(value, str):
            yield value
    children = getattr(element, "children", None)
    if isinstance(children, dict):
        children = children.values()
    if children:
        for child in children:
            yield from _visible_strings(child)


def test_format_locations_for_supported_document_types():
    cases = {
        "PDF · page 2": {"page": 2},
        "DOCX · paragraph 4 · Operations > Decisions": {
            "paragraph": 4, "section": "Operations > Decisions",
        },
        "DOCX · table 1, row 3, cell 2": {"table": 1, "row": 3, "cell": 2},
        "XLSX · worksheet Campaign Budget, row 5": {"worksheet": "Campaign Budget", "row": 5},
        "CSV · record 3, lines 4–6": {"record": 3, "line_start": 4, "line_end": 6},
        "Line 7": {"line": 7},
    }
    for expected, location in cases.items():
        assert format_source_location(location) == expected


def test_format_markdown_line_and_xlsx_header_locations():
    assert format_source_location({"line": 8, "section": "Campaign > Budget"}) == "Line 8"
    assert format_source_location({"worksheet": "Invoices", "kind": "worksheet_header"}) == (
        "XLSX · worksheet Invoices (header)"
    )


def test_old_and_invalid_citations_are_safe_and_explicitly_locationless():
    old = prepare_citation_display({
        "source": "drive", "source_id": "old", "title": "Legacy file",
        "excerpt": "Evidence text", "source_locations": None,
    })
    assert old["locations"] == []
    assert old["excerpt"] == "Evidence text"
    assert format_source_location({"page": -2}) is None
    assert format_source_location({"line_start": 8, "line_end": 2}) is None
    assert format_source_location({"path": "C:\\Users\\private\\file.pdf"}) is None
    invalid = prepare_citation_display({"source_locations": "not-a-list"})
    assert invalid["locations"] == []


def test_multi_facet_display_keeps_support_status_and_facet_citations_separate():
    facets = prepare_facet_display([
        {"facet": "quotation amount", "status": "supported", "citations": [{
            "source": "drive", "source_id": "quote", "title": "Quote.pdf",
            "excerpt": "Estimated amount: INR 500", "source_locations": [{"page": 1}],
        }]},
        {"facet": "customer phone number", "status": "insufficient_evidence", "citations": []},
    ])
    assert facets[0]["status"] == "Supported"
    assert facets[0]["citations"][0]["locations"] == ["PDF · page 1"]
    assert facets[1]["status"] == "Insufficient evidence"
    assert facets[1]["citations"] == []


def _patch_settings(monkeypatch, database_path):
    def make_settings():
        return SimpleNamespace(
            database_path=str(database_path),
            output_dir=str(database_path.parent / "outputs"),
            google_client_secrets_file="missing-local-test-credentials.json",
            gmail_mailbox_owner="",
            google_drive_account="",
            selected_whatsapp_groups=lambda: set(),
            reference_day=lambda: date(2026, 10, 31),
        )

    monkeypatch.setattr(config, "Settings", make_settings)


def test_dashboard_renders_citation_locations_and_unsupported_facet(tmp_path, monkeypatch):
    database_path = tmp_path / "dashboard-citations.sqlite"
    _patch_settings(monkeypatch, database_path)
    conn = connect(str(database_path))
    text = "Rental quotation amount: INR 500."
    upsert_record(
        conn, source="drive", source_id="quote", title="Synthetic quotation.pdf",
        content=text,
        metadata_json=json.dumps({"source_locations": [{
            "start": 0, "end": len(text), "location": {"page": 1},
        }]}),
    )
    conn.commit()
    conn.close()

    app_test = AppTest.from_file(str(APP_PATH)).run()
    question_input = next(item for item in app_test.text_input
                          if item.label == "Ask about collected records")
    question_input.set_value(
        "What is the rental quotation amount and customer phone number?"
    )
    app_test.button(key="ask_business_question").click()
    app_test = app_test.run()
    assert not app_test.exception
    visible_parts = list(_visible_strings(app_test.main))
    visible = "\n".join(visible_parts)
    assert "Evidence by requested facet" in visible
    assert "quotation amount" in visible
    assert "Supported" in visible
    assert "Insufficient evidence" in visible
    assert "PDF · page 1" in visible
    assert "Evidence excerpt" in visible
