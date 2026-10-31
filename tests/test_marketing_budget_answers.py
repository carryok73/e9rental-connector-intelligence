from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from src.connector_intelligence import config
from src.connector_intelligence.connectors.local_drive import import_local_folder
from src.connector_intelligence.db import connect, upsert_record
from src.connector_intelligence.marketing_budget import (
    extract_campaign_budget,
    is_marketing_budget_question,
    requested_budget_view,
    summarize_campaign_budget,
)
from src.connector_intelligence.query import answer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "data" / "imports" / "drive_sample"
APP_PATH = PROJECT_ROOT / "app.py"


def _sheet_text(rows):
    lines = [
        "Worksheet: Campaign Budget",
        "Campaign | Channel | Planned Expenditure (INR) | Actual Expenditure (INR) | Performance",
        *rows,
    ]
    text = "\n".join(lines)
    locations = []
    offset = 0
    for row_number, line in enumerate(lines[1:], start=1):
        locations.append({"start": offset + (len(lines[0]) + 1 if row_number == 1 else 0),
                          "end": offset + (len(lines[0]) + 1 if row_number == 1 else 0) + len(line),
                          "location": {"worksheet": "Campaign Budget", "row": row_number}})
        offset += len(line) + 1
    # Locate each text line directly so offsets remain correct if fixtures change.
    locations = []
    offset = 0
    for row_number, line in enumerate(lines[1:], start=1):
        start = len(lines[0]) + 1 if row_number == 1 else offset
        if row_number == 1:
            offset = start
        locations.append({"start": start, "end": start + len(line),
                          "location": {"worksheet": "Campaign Budget", "row": row_number}})
        offset = start + len(line) + 1
    return text, json.dumps({"source_locations": locations})


def _actual_sample(conn):
    report = import_local_folder(SAMPLE_ROOT, "m10b-test", conn)
    assert report["counts"]["imported"] == 5


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


def test_sample_workbook_campaign_fields_and_row_locations_are_extracted():
    conn = connect(":memory:")
    _actual_sample(conn)
    result = answer(conn, "Show the marketing campaign budget")
    document = result["marketing_answer"]["documents"][0]
    assert [row["campaign"] for row in document["campaigns"]] == [
        "Monsoon Mobility", "Weekend Wheels", "City Explorer", "Fleet Saver", "Autumn Roadshow",
    ]
    assert [row["source_locations"][0] for row in document["campaigns"]] == [
        {"worksheet": "Campaign Budget", "row": 2},
        {"worksheet": "Campaign Budget", "row": 3},
        {"worksheet": "Campaign Budget", "row": 4},
        {"worksheet": "Campaign Budget", "row": 5},
        {"worksheet": "Campaign Budget", "row": 6},
    ]
    assert result["citations"][0]["source_locations"] == [
        {"worksheet": "Campaign Budget", "row": row} for row in range(2, 7)
    ]
    assert all("Total |" not in row["evidence"] for row in document["campaigns"])
    conn.close()


def test_planned_actual_expenditure_and_performance_are_extracted():
    conn = connect(":memory:")
    _actual_sample(conn)
    result = answer(conn, "What is the planned expenditure?")
    campaigns = result["marketing_answer"]["documents"][0]["campaigns"]
    assert campaigns[0]["planned_expenditure"]["value"] == "INR 42,000"
    assert campaigns[0]["planned_expenditure"]["status"] == "extracted"
    assert campaigns[0]["actual_expenditure"]["value"] == "INR 39,800"
    assert campaigns[0]["channel"] == "Search advertising"
    assert campaigns[0]["performance"] == "1,240 visits; 86 enquiry forms"
    assert campaigns[1]["actual_expenditure"]["value"] == "INR 31,800"
    conn.close()


def test_variances_totals_and_budget_comparisons_are_calculated():
    conn = connect(":memory:")
    _actual_sample(conn)
    result = answer(conn, "Compare planned and actual expenditure")
    doc = result["marketing_answer"]["documents"][0]
    assert doc["campaigns"][0]["variance"]["value"] == "INR -2,200"
    assert doc["campaigns"][0]["variance"]["status"] == "calculated"
    assert doc["calculations"]["planned_total"]["value"] == "INR 151,000"
    assert doc["calculations"]["actual_total"]["value"] == "INR 147,550"
    assert doc["calculations"]["variance_total"]["value"] == "INR -3,450"
    assert doc["calculations"]["planned_total"]["status"] == "calculated"
    assert doc["calculations"]["planned_total"]["source_locations"] == [
        {"worksheet": "Campaign Budget", "row": row} for row in range(2, 7)
    ]
    above = answer(conn, "Which campaigns exceeded their budget?")
    assert "Weekend Wheels, Autumn Roadshow" in above["answer"]
    below = answer(conn, "Which campaigns spent below budget?")
    assert "Monsoon Mobility, City Explorer, Fleet Saver" in below["answer"]
    conn.close()


def test_total_query_wording_and_supported_question_variations():
    questions = [
        "Show the marketing campaign budget",
        "What is the planned expenditure?",
        "What is the actual expenditure?",
        "Which campaigns exceeded their budget?",
        "Compare planned and actual expenditure",
        "What is the total planned budget?",
        "What is the total actual expenditure?",
        "Show all campaign expenditure details",
    ]
    assert all(is_marketing_budget_question(question) for question in questions)
    assert requested_budget_view(questions[5]) == "total_planned"
    assert requested_budget_view(questions[6]) == "total_actual"
    conn = connect(":memory:")
    _actual_sample(conn)
    planned = answer(conn, questions[5])
    actual = answer(conn, questions[6])
    assert planned["answer"] == (
        "document_003.xlsx: Total planned expenditure: INR 151,000 (calculated from 5 of 5 campaign rows)."
    )
    assert "INR 147,550" in actual["answer"]
    conn.close()


def test_missing_and_malformed_cells_do_not_create_false_variances_or_totals():
    text, metadata = _sheet_text([
        "Alpha | Search | 1,000 | 900 | 5 leads",
        "Beta | Social | NA | 500 | 8 clicks",
        "Gamma | Email | 300 |  | 12% open rate",
        "Grand total | | 1,300 | 1,400 | summary",
    ])
    extracted = extract_campaign_budget(text, metadata)["campaigns"]
    assert [item["campaign"] for item in extracted] == ["Alpha", "Beta", "Gamma"]
    assert extracted[0]["variance"]["value"] == "INR -100"
    assert extracted[1]["planned_expenditure"]["status"] == "invalid"
    assert extracted[1]["variance"]["status"] == "missing"
    assert extracted[2]["actual_expenditure"] is None
    summary, status, totals = summarize_campaign_budget(extracted, "total_planned")
    assert status == "partially_supported"
    assert "partially calculated from 2 of 3" in summary
    assert totals["planned_total"]["source_locations"] == [
        {"worksheet": "Campaign Budget", "row": 2},
        {"worksheet": "Campaign Budget", "row": 4},
    ]


def test_unrelated_spreadsheet_is_not_treated_as_campaign_budget():
    conn = connect(":memory:")
    content = "Worksheet: Staff Roster\nName | Shift | Hours\nAvery | Morning | 8"
    upsert_record(conn, source="drive", source_id="roster", title="Roster.xlsx", content=content)
    result = answer(conn, "Show the marketing campaign budget")
    assert "marketing_answer" not in result
    assert all(item.get("source_id") != "roster" for item in result["citations"])
    conn.close()


def test_dashboard_shows_budget_table_and_row_level_worksheet_citations(tmp_path, monkeypatch):
    database_path = tmp_path / "campaign-dashboard.sqlite"
    monkeypatch.setattr(config, "Settings", lambda: SimpleNamespace(
        database_path=str(database_path), output_dir=str(tmp_path / "outputs"),
        google_client_secrets_file="missing.json", gmail_mailbox_owner="",
        google_drive_account="", selected_whatsapp_groups=lambda: set(),
        reference_day=lambda: date(2026, 10, 31),
    ))
    conn = connect(str(database_path))
    _actual_sample(conn)
    conn.commit()
    conn.close()
    app_test = AppTest.from_file(str(APP_PATH)).run()
    question = next(item for item in app_test.text_input
                    if item.label == "Ask about collected records")
    question.set_value("Which campaigns exceeded their budget?")
    app_test.button(key="ask_business_question").click()
    app_test = app_test.run()
    assert not app_test.exception
    visible = "\n".join(_visible_strings(app_test.main))
    assert "Marketing campaign budget" in visible
    assert "Evidence status: Supported" in visible
    assert "Campaign Budget" in visible
    assert "worksheet Campaign Budget, row 3" in visible
    conn.close()


def test_existing_rental_deterministic_answers_remain_unchanged():
    conn = connect(":memory:")
    _actual_sample(conn)
    result = answer(conn, "What is the rental duration?")
    assert result["answer"] == "document_001.pdf: Rental duration: 4 days."
    assert result["citations"][0]["source_locations"] == [{"page": 1}]
    assert "marketing_answer" not in result
    conn.close()
