from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from src.connector_intelligence import config
from src.connector_intelligence.connectors.local_drive import import_local_folder
from src.connector_intelligence.db import connect, upsert_record
from src.connector_intelligence.query import answer
from src.connector_intelligence.rental_quote import (
    extract_rental_quotation,
    is_rental_quotation_document,
    is_rental_quotation_question,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "data" / "imports" / "drive_sample"
APP_PATH = PROJECT_ROOT / "app.py"


def _quote_text(*, duration="5 days", start="1 March 2026", end="6 March 2026",
                include_optional=True, include_amount=True):
    lines = [
        "VEHICLE RENTAL QUOTATION",
        "Quotation date: 20 February 2026 | Quote: RQ-9001",
    ]
    if include_optional:
        lines.extend([
            "Prepared for: Synthetic Example Customer | Customer reference: TEST-CUST-09",
            "Vehicle: 2025 Example Touring, automatic, 5 seats",
        ])
    period = f"Rental period: {start} through {end}"
    if duration is not None:
        period += f" ({duration})"
    lines.append(period)
    if include_amount:
        lines.append("Estimated total: USD 1,250.00")
    return "\n".join(lines)


def _page_locations(text, page_spans):
    locations = []
    offset = 0
    for index, segment in enumerate(page_spans, start=1):
        if index > 1:
            offset += 1  # extract_text joins PDF pages with one newline
        locations.append({
            "start": offset, "end": offset + len(segment),
            "location": {"page": index},
        })
        offset += len(segment)
    assert "\n".join(page_spans) == text
    return locations


def _insert_quote(conn, text, *, pages=None, source="drive", source_id="quote-1", title="synthetic.pdf"):
    if pages is None:
        pages = [text]
    metadata = {"source_locations": _page_locations(text, pages)}
    upsert_record(conn, source=source, source_id=source_id, title=title,
                  content=text, category="sales", metadata_json=json.dumps(metadata))


def _field(result, key):
    return next(field for field in result["rental_answer"]["documents"][0]["fields"]
                if field["key"] == key)


def test_rental_duration_and_amount_are_extracted_from_retrieved_quote():
    conn = connect(":memory:")
    import_local_folder(SAMPLE_ROOT, "quote-sample", conn)

    duration = answer(conn, "What is the rental duration?")
    amount = answer(conn, "What is the quotation amount?")

    assert duration["answer"] == "document_001.pdf: Rental duration: 4 days."
    assert _field(duration, "rental_duration")["status"] == "supported"
    assert _field(amount, "estimated_total_amount")["value"] == "INR 14,600"
    assert _field(amount, "estimated_total_amount")["evidence"] == "Estimated total: INR 14,600"
    assert amount["citations"][0]["source_locations"] == [{"page": 1}]
    conn.close()


def test_all_requested_quotation_fields_have_value_evidence_and_page_citation():
    conn = connect(":memory:")
    import_local_folder(SAMPLE_ROOT, "quote-details", conn)

    result = answer(conn, "Show the rental quotation details.")
    fields = {field["key"]: field for field in result["rental_answer"]["documents"][0]["fields"]}

    assert result["rental_answer"]["status"] == "supported"
    assert fields["quotation_number"]["value"] == "Q-7421"
    assert fields["quotation_date"]["value"] == "18 September 2026"
    assert fields["customer_name"]["value"] == "Mira Solen"
    assert fields["customer_reference"]["value"] == "SYN-CUST-014"
    assert fields["vehicle_details"]["value"] == "2024 Northstar Comet, compact sedan, automatic, 5 seats"
    assert fields["rental_start_date"]["value"] == "12 October 2026"
    assert fields["rental_end_date"]["value"] == "16 October 2026"
    assert fields["rental_duration"]["value"] == "4 days"
    assert fields["estimated_total_amount"]["value"] == "INR 14,600"
    assert all(field["status"] == "supported" and field["evidence"]
               and field["source_locations"] == [{"page": 1}]
               for field in fields.values())
    assert result["citations"][0]["source_locations"] == [{"page": 1}]
    conn.close()


@pytest.mark.parametrize("question", [
    "What is the rental duration?",
    "How long is the rental period?",
    "What is the quotation amount?",
    "When does the rental start and end?",
    "Show the rental quotation details.",
    "What is the quotation number?",
    "Who is the customer on the rental quote?",
    "What vehicle is listed in the quotation?",
])
def test_question_wording_variations_use_deterministic_quotation_path(question):
    assert is_rental_quotation_question(question)


def test_quote_number_customer_vehicle_and_date_patterns_parse_generic_values():
    text = _quote_text()
    parsed = extract_rental_quotation(text, json.dumps({"source_locations": [
        {"start": 0, "end": len(text), "location": {"page": 2}},
    ]}))
    fields = parsed["fields"]
    assert fields["quotation_number"]["value"] == "RQ-9001"
    assert fields["quotation_date"]["value"] == "20 February 2026"
    assert fields["customer_name"]["value"] == "Synthetic Example Customer"
    assert fields["customer_reference"]["value"] == "TEST-CUST-09"
    assert fields["vehicle_details"]["value"] == "2025 Example Touring, automatic, 5 seats"
    assert fields["rental_start_date"]["value"] == "1 March 2026"
    assert fields["rental_end_date"]["value"] == "6 March 2026"


def test_missing_fields_are_explicit_and_not_invented():
    text = _quote_text(include_optional=False, include_amount=False)
    fields = extract_rental_quotation(text)["fields"]
    assert fields["customer_name"]["value"] is None
    assert fields["customer_name"]["status"] == "missing"
    assert fields["estimated_total_amount"]["value"] is None
    assert fields["estimated_total_amount"]["status"] == "missing"
    assert fields["quotation_number"]["value"] == "RQ-9001"


def test_malformed_or_ambiguous_dates_are_not_reported_as_supported():
    text = _quote_text(start="31 February 2026", end="6 March 2026", duration=None)
    fields = extract_rental_quotation(text)["fields"]
    assert fields["rental_start_date"]["value"] is None
    assert fields["rental_start_date"]["status"] == "invalid"
    assert fields["rental_end_date"]["status"] == "supported"
    assert fields["rental_duration"]["status"] == "missing"

    ambiguous = _quote_text(start="03/04/2026", end="03/09/2026", duration=None)
    ambiguous_fields = extract_rental_quotation(ambiguous)["fields"]
    assert ambiguous_fields["rental_start_date"]["status"] == "invalid"
    assert ambiguous_fields["rental_end_date"]["status"] == "invalid"


def test_stated_duration_is_preferred_but_conflict_is_reported():
    fields = extract_rental_quotation(_quote_text(duration="4 days"))["fields"]
    assert fields["rental_duration"]["status"] == "conflict"
    assert "states 4 days" in fields["rental_duration"]["note"]
    assert "5 elapsed calendar days" in fields["rental_duration"]["note"]


def test_date_difference_is_labeled_elapsed_only_when_duration_is_not_stated():
    field = extract_rental_quotation(_quote_text(duration=None))["fields"]["rental_duration"]
    assert field["status"] == "calculated"
    assert field["value"] == "5 elapsed calendar days"
    assert "does not specify inclusive or exclusive" in field["note"]


def test_field_span_maps_to_its_own_pdf_page_not_every_document_page():
    page_one = "VEHICLE RENTAL QUOTATION\nQuotation date: 20 February 2026 | Quote: RQ-9001"
    page_two = "Rental period: 1 March 2026 through 6 March 2026 (5 days)\nEstimated total: USD 1,250.00"
    text = page_one + "\n" + page_two
    conn = connect(":memory:")
    _insert_quote(conn, text, pages=[page_one, page_two])

    result = answer(conn, "What is the quotation amount?")

    amount = _field(result, "estimated_total_amount")
    assert amount["source_locations"] == [{"page": 2}]
    assert result["citations"][0]["source_locations"] == [{"page": 2}]
    assert result["citations"][0]["excerpt"] == "Estimated total: USD 1,250.00"
    assert amount["start"] == text.index("USD 1,250.00")
    assert text[amount["start"]:amount["end"]] == "USD 1,250.00"
    conn.close()


def test_unrelated_records_and_non_drive_quotes_are_not_used():
    conn = connect(":memory:")
    unrelated = "Rental quotation is mentioned in these notes, but no quote fields are included."
    _insert_quote(conn, unrelated, source_id="unstructured", title="rental-notes.pdf")
    valid_text = _quote_text()
    _insert_quote(conn, valid_text, source="gmail", source_id="mail-quote", title="Rental quote email")

    result = answer(conn, "What is the rental duration?")

    assert result["rental_answer"]["status"] == "insufficient_evidence"
    assert result["citations"] == []
    assert not is_rental_quotation_document(unrelated)
    conn.close()


def test_malformed_location_metadata_keeps_field_but_marks_location_unavailable():
    text = _quote_text()
    fields = extract_rental_quotation(text, "malformed-json")["fields"]
    assert fields["rental_duration"]["value"] == "5 days"
    assert fields["rental_duration"]["status"] == "supported"
    assert fields["rental_duration"]["source_locations"] == []
    assert fields["rental_duration"]["location_status"] == "unavailable"


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


def test_dashboard_shows_quote_fields_evidence_status_and_pdf_page(tmp_path, monkeypatch):
    database_path = tmp_path / "rental-dashboard.sqlite"
    monkeypatch.setattr(config, "Settings", lambda: SimpleNamespace(
        database_path=str(database_path), output_dir=str(tmp_path / "outputs"),
        google_client_secrets_file="missing.json", gmail_mailbox_owner="",
        google_drive_account="", selected_whatsapp_groups=lambda: set(),
        reference_day=lambda: date(2026, 10, 31),
    ))
    conn = connect(str(database_path))
    text = _quote_text()
    _insert_quote(conn, text, pages=[text])
    conn.commit()
    conn.close()

    app_test = AppTest.from_file(str(APP_PATH)).run()
    app_test.button(key="question_example_0").click()
    app_test = app_test.run()
    question = next(item for item in app_test.text_input
                    if item.label == "Ask about collected records")
    assert question.value == "What is the rental duration?"
    app_test.button(key="ask_business_question").click()
    app_test = app_test.run()
    assert not app_test.exception
    visible = "\n".join(_visible_strings(app_test.main))
    assert "Rental quotation" in visible
    assert "Rental duration: 5 days." in visible
    assert "Rental duration" in visible
    assert "Source document" in visible
    assert "PDF" in visible and "page 1" in visible
    assert "Evidence status: Supported" in visible
