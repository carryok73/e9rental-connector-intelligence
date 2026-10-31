from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from src.connector_intelligence import config
from src.connector_intelligence.connectors.drive import extract_text_with_locations
from src.connector_intelligence.connectors.local_drive import import_local_folder
from src.connector_intelligence.db import connect, upsert_record
from src.connector_intelligence.query import answer
from src.connector_intelligence.verification import (
    extract_verification_records,
    is_verification_document,
    is_verification_question,
    overall_verification_status,
    requested_verification_view,
    summarize_verification,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "data" / "imports" / "drive_sample"
APP_PATH = PROJECT_ROOT / "app.py"


def _checklist(rows):
    lines = [
        "CUSTOMER VERIFICATION CHECKLIST",
        "Document type | Verification status | Notes",
        *rows,
    ]
    text = "\n".join(lines)
    locations = []
    offset = 0
    for row_number, line in enumerate(lines, start=1):
        locations.append({"start": offset, "end": offset + len(line),
                          "location": {"line": row_number}})
        offset += len(line) + 1
    return text, json.dumps({"source_locations": locations})


def _sample_result(conn, question):
    report = import_local_folder(SAMPLE_ROOT, "m10c-test", conn)
    assert report["counts"]["imported"] == 5
    return answer(conn, question)


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


def test_sample_verification_rows_extract_explicit_statuses_notes_and_txt_lines():
    path = SAMPLE_ROOT / "document_004.txt"
    detailed = extract_text_with_locations(path.read_bytes(), path.name)
    extracted = extract_verification_records(
        detailed["text"], json.dumps({"source_locations": detailed["locations"]})
    )
    assert extracted["is_verification_document"] is True
    assert [(item["document_name"], item["status"]) for item in extracted["records"]] == [
        ("Government identity document", "verified"),
        ("Driving licence", "pending"),
        ("Proof of address", "pending"),
        ("Customer consent form", "verified"),
    ]
    assert extracted["records"][1]["notes"] == "Awaiting a readable copy from the fictional customer"
    assert extracted["records"][2]["notes"] == "Address details intentionally omitted"
    assert "not to treat them as real customer verification" in extracted["source_note"]["text"]
    assert extracted["source_note"]["source_locations"] == [{"line": 13}]
    assert [item["source_locations"] for item in extracted["records"]] == [
        [{"line": 8}], [{"line": 9}], [{"line": 10}], [{"line": 11}],
    ]
    assert all(item["status_evidence"] for item in extracted["records"])


def test_explicit_rejected_failed_and_unknown_statuses_are_handled():
    text, metadata = _checklist([
        "Identity card | Rejected | Image unreadable",
        "Driving permit | Failed | Signature missing",
        "Address record | | Not reviewed",
        "Consent | Received | No verification label supplied",
        "Other ID | Not verified | Awaiting review",
    ])
    extracted = extract_verification_records(text, metadata)
    assert [item["status"] for item in extracted["records"]] == [
        "rejected", "failed", "unknown", "unknown", "unknown",
    ]
    assert extracted["records"][2]["status_evidence"] is None
    assert extracted["records"][3]["status"] == "unknown"


def test_overall_status_requires_explicit_support_and_tracks_incomplete_records():
    text, metadata = _checklist([
        "Identity card | Verified | Readable",
        "Driving permit | Pending | Copy requested",
    ])
    records = extract_verification_records(text, metadata)["records"]
    overall = overall_verification_status(records, text)
    assert overall["status"] == "incomplete"
    assert overall["source_locations"] == [{"line": 4}]
    summary, evidence_status = summarize_verification(records, "overall", overall)
    assert "Overall status: Incomplete" in summary
    assert "does not establish that the checklist includes every requirement" in summary
    assert evidence_status == "supported"

    verified_text, verified_metadata = _checklist([
        "Identity card | Verified | Good",
        "Consent | Verified | Recorded",
    ])
    verified_records = extract_verification_records(verified_text, verified_metadata)["records"]
    assert overall_verification_status(verified_records, verified_text)["status"] == "unable_to_determine"
    complete_text = verified_text + "\nAll required documents are verified; checklist is complete."
    assert overall_verification_status(verified_records, complete_text)["status"] == "completed"

    unknown_text, unknown_metadata = _checklist([
        "Identity card | Pending | Copy requested",
        "Address record | | Not reviewed",
    ])
    unknown_records = extract_verification_records(unknown_text, unknown_metadata)["records"]
    assert overall_verification_status(unknown_records, unknown_text)["status"] == "unable_to_determine"

    rejected_text, rejected_metadata = _checklist(["Identity card | Rejected | Unreadable"])
    rejected_records = extract_verification_records(rejected_text, rejected_metadata)["records"]
    assert overall_verification_status(rejected_records, rejected_text)["status"] == "action_required"


def test_all_supported_question_wordings_are_recognized_and_specific_views_selected():
    questions = [
        "What is the customer's verification status?",
        "Show customer verification details",
        "Which documents are verified?",
        "Which documents are pending?",
        "Is the driving licence verified?",
        "Is proof of address verified?",
        "Are all verification requirements completed?",
    ]
    assert all(is_verification_question(question) for question in questions)
    assert requested_verification_view(questions[2]) == "verified"
    assert requested_verification_view(questions[3]) == "pending"
    assert requested_verification_view(questions[4]) == "driving_licence"
    assert requested_verification_view(questions[5]) == "proof_of_address"
    assert requested_verification_view(questions[6]) == "overall"
    assert not is_verification_question("What is the invoice payment status?")
    assert not is_verification_question("What is the passport number on the verification record?")


def test_query_returns_sample_verification_statuses_with_precise_line_citations():
    conn = connect(":memory:")
    result = _sample_result(conn, "Show customer verification details")
    document = result["verification_answer"]["documents"][0]
    assert result["verification_answer"]["status"] == "supported"
    assert document["overall"]["status"] == "incomplete"
    assert "Driving licence: Pending" in result["answer"]
    assert "Proof of address: Pending" in result["answer"]
    assert "does not establish that the checklist includes every requirement" in result["answer"]
    assert result["citations"][0]["title"] == "document_004.txt"
    assert result["citations"][0]["source_locations"] == [
        {"line": 8}, {"line": 9}, {"line": 10}, {"line": 11}, {"line": 13},
    ]
    conn.close()


def test_verified_pending_and_specific_document_queries_cite_only_matching_rows():
    conn = connect(":memory:")
    _sample_result(conn, "Show customer verification details")
    verified = answer(conn, "Which documents are verified?")
    assert "Government identity document: Verified" in verified["answer"]
    assert "Customer consent form: Verified" in verified["answer"]
    assert verified["citations"][0]["source_locations"] == [
        {"line": 8}, {"line": 11}, {"line": 13},
    ]

    pending = answer(conn, "Which documents are pending?")
    assert "Driving licence: Pending" in pending["answer"]
    assert "Proof of address: Pending" in pending["answer"]
    assert pending["citations"][0]["source_locations"] == [
        {"line": 9}, {"line": 10}, {"line": 13},
    ]

    driving = answer(conn, "Is the driving licence verified?")
    assert "Driving licence: Pending." in driving["answer"]
    assert driving["citations"][0]["source_locations"] == [{"line": 9}, {"line": 13}]
    address = answer(conn, "Is proof of address verified?")
    assert "Proof of address: Pending." in address["answer"]
    assert address["citations"][0]["source_locations"] == [{"line": 10}, {"line": 13}]
    conn.close()


def test_all_requirements_question_reports_incomplete_without_claiming_exhaustive_coverage():
    conn = connect(":memory:")
    result = _sample_result(conn, "Are all verification requirements completed?")
    assert "Overall status: Incomplete" in result["answer"]
    assert result["verification_answer"]["documents"][0]["overall"]["status"] == "incomplete"
    assert result["citations"][0]["source_locations"] == [
        {"line": 8}, {"line": 9}, {"line": 10}, {"line": 11}, {"line": 13},
    ]
    conn.close()


def test_unrelated_content_is_not_recognized_as_verification_document():
    text = "Meeting Notes\nOwner | Status | Task\nTeam A | Pending | Finish the customer dashboard"
    assert not is_verification_document(text)
    conn = connect(":memory:")
    upsert_record(conn, source="drive", source_id="meeting", title="Notes.txt", content=text)
    result = answer(conn, "Which documents are pending?")
    assert "verification_answer" not in result
    assert all(item.get("source_id") != "meeting" for item in result["citations"])
    conn.close()


def test_unsupported_verification_field_does_not_generate_a_status_answer():
    conn = connect(":memory:")
    _sample_result(conn, "Show customer verification details")
    result = answer(conn, "What is the passport number on the verification record?")
    assert "verification_answer" not in result
    assert "Government identity document: Verified" not in result["answer"]
    conn.close()


def test_milestone_10a_and_10b_answers_remain_available():
    conn = connect(":memory:")
    _sample_result(conn, "Show customer verification details")
    rental = answer(conn, "What is the rental duration?")
    campaign = answer(conn, "What is the total planned budget?")
    assert rental["answer"] == "document_001.pdf: Rental duration: 4 days."
    assert rental["citations"][0]["source_locations"] == [{"page": 1}]
    assert campaign["marketing_answer"]["status"] == "supported"
    assert "INR 151,000" in campaign["answer"]
    assert "verification_answer" not in rental
    assert "verification_answer" not in campaign
    conn.close()


def test_missing_verification_rows_report_insufficient_evidence():
    conn = connect(":memory:")
    upsert_record(conn, source="drive", source_id="empty-checklist", title="Checklist.txt",
                  content="Customer verification checklist\nNo document rows recorded.")
    result = answer(conn, "What is the customer's verification status?")
    assert "verification_answer" not in result
    assert "citations" in result
    conn.close()


def test_dashboard_displays_verification_status_evidence_and_txt_lines(tmp_path, monkeypatch):
    database_path = tmp_path / "verification-dashboard.sqlite"
    monkeypatch.setattr(config, "Settings", lambda: SimpleNamespace(
        database_path=str(database_path), output_dir=str(tmp_path / "outputs"),
        google_client_secrets_file="missing.json", gmail_mailbox_owner="",
        google_drive_account="", selected_whatsapp_groups=lambda: set(),
        reference_day=lambda: date(2026, 10, 31),
    ))
    conn = connect(str(database_path))
    _sample_result(conn, "Show customer verification details")
    conn.commit()
    conn.close()
    app_test = AppTest.from_file(str(APP_PATH)).run()
    question = next(item for item in app_test.text_input
                    if item.label == "Ask about collected records")
    question.set_value("Are all verification requirements completed?")
    app_test.button(key="ask_business_question").click()
    app_test = app_test.run()
    assert not app_test.exception
    visible = "\n".join(_visible_strings(app_test.main))
    assert "Customer verification summary" in visible
    assert "Overall verification status" in visible and "Incomplete" in visible
    assert "View source evidence" in visible
    assert any("Line 9" in str(item.value) and "Line 10" in str(item.value)
               for item in app_test.dataframe)
