import json

from src.connector_intelligence.db import connect, read_source_records, upsert_record
from src.connector_intelligence.exports import export_all, export_source
from src.connector_intelligence.query import answer


EXPORT_FIELDS = {
    "source_id", "parent_id", "occurred_at", "title", "content", "category", "metadata",
}


def test_synthetic_records_search_cite_and_export_across_sources(tmp_path):
    conn = connect(":memory:")
    selected_group = "synthetic-group-selected"
    removed_group = "synthetic-group-unselected"

    upsert_record(
        conn,
        source="whatsapp",
        source_id="synthetic-wa-selected-001",
        parent_id=selected_group,
        occurred_at="1760000000",
        title="Synthetic rental schedule discussion",
        content="Synthetic rental schedule approved for the test fixture.",
        metadata_json=json.dumps({
            "sender": "synthetic-sender-001",
            "message_type": "text",
            "interpretation": {
                "summary": "Synthetic rental schedule approved for the test fixture.",
                "requests_and_commitments": [],
                "action_items": [],
                "decisions": ["Synthetic rental schedule approved for the test fixture."],
                "method": "local keyword and sentence heuristics; candidates require human review",
            },
        }),
    )
    upsert_record(
        conn,
        source="whatsapp",
        source_id="synthetic-wa-unselected-002",
        parent_id=removed_group,
        occurred_at="1760000001",
        title="Synthetic rental schedule discussion",
        content="Synthetic rental schedule exists in an unselected test group.",
        metadata_json=json.dumps({
            "sender": "synthetic-sender-002",
            "message_type": "text",
            "interpretation": {
                "summary": "Synthetic rental schedule exists in an unselected test group.",
                "requests_and_commitments": [],
                "action_items": [],
                "decisions": ["Synthetic rental schedule exists in an unselected test group."],
                "method": "local keyword and sentence heuristics; candidates require human review",
            },
        }),
    )
    upsert_record(
        conn,
        source="gmail",
        source_id="synthetic-gmail-001",
        parent_id="synthetic-thread-001",
        occurred_at="2026-01-15T12:00:00+00:00",
        title="Synthetic rental schedule email",
        content="Synthetic rental schedule details for offline integration testing.",
        category="sales",
        metadata_json=json.dumps({
            "sender": "synthetic-sender@example.test",
            "is_inbound": True,
            "is_unanswered": False,
            "unanswered_reference_date": "2026-01-20",
            "unanswered_days": 5,
            "attachments": [{"filename": "synthetic-quote.pdf", "mime_type": "application/pdf"}],
            "attachment_flags": [{
                "filename": "synthetic-quote.pdf",
                "category": "quotation",
                "reason": "Synthetic test attachment filename.",
            }],
            "classification_reason": "Synthetic test classification.",
        }),
    )
    upsert_record(
        conn,
        source="drive",
        source_id="synthetic-drive-001",
        parent_id="synthetic-folder-001",
        occurred_at="2026-01-14T08:30:00Z",
        title="Synthetic rental schedule document",
        content="Synthetic rental schedule details in a local test record.",
        category="office",
        metadata_json=json.dumps({
            "status": "extracted",
            "category_reason": "Synthetic test document classification.",
            "web_view_link": "https://drive.google.com/file/d/synthetic-drive-001/view",
            "extracted_characters": 54,
            "extraction_error": None,
        }),
    )

    selected_whatsapp_rows = read_source_records(conn, "whatsapp", {selected_group})
    assert [row["source_id"] for row in selected_whatsapp_rows] == ["synthetic-wa-selected-001"]

    selected_result = answer(conn, "synthetic rental schedule", {selected_group})
    selected_citations = {
        (citation["source_type"], citation["source_id"])
        for citation in selected_result["citations"]
    }
    assert selected_citations == {
        ("whatsapp", "synthetic-wa-selected-001"),
        ("gmail", "synthetic-gmail-001"),
        ("drive", "synthetic-drive-001"),
    }
    assert all(citation["timestamp"] for citation in selected_result["citations"])
    citations_by_source = {citation["source_type"]: citation for citation in selected_result["citations"]}
    assert citations_by_source["whatsapp"]["group_context"] == selected_group
    assert citations_by_source["gmail"]["thread_id"] == "synthetic-thread-001"
    assert citations_by_source["gmail"]["sender"] == "synthetic-sender@example.test"
    assert citations_by_source["drive"]["drive_link"] == "https://drive.google.com/file/d/synthetic-drive-001/view"
    assert "not a verified analytical answer" in selected_result["answer"]

    candidate_result = answer(conn, "Show potential decisions in WhatsApp.", {selected_group})
    assert [item["source_id"] for item in candidate_result["citations"]] == ["synthetic-wa-selected-001"]
    assert all(item["source_id"] != "synthetic-wa-unselected-002" for item in candidate_result["citations"])

    output_dir = tmp_path / "synthetic-exports"
    export_all(conn, output_dir, selected_whatsapp_groups={selected_group})
    exported = {}
    for source in ("whatsapp", "gmail", "drive"):
        path = output_dir / f"{source}_output.json"
        records = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(records, list)
        assert all(set(record) == EXPORT_FIELDS for record in records)
        exported[source] = records
    assert [record["source_id"] for record in exported["whatsapp"]] == ["synthetic-wa-selected-001"]
    assert [record["source_id"] for record in exported["gmail"]] == ["synthetic-gmail-001"]
    assert [record["source_id"] for record in exported["drive"]] == ["synthetic-drive-001"]
    assert exported["gmail"][0]["metadata"]["attachment_flags"][0]["category"] == "quotation"
    assert exported["drive"][0]["metadata"]["category_reason"] == "Synthetic test document classification."

    no_whatsapp_result = answer(conn, "synthetic rental schedule", set())
    no_whatsapp_sources = {item["source_type"] for item in no_whatsapp_result["citations"]}
    assert no_whatsapp_sources == {"gmail", "drive"}
    assert all(item["source_type"] != "whatsapp" for item in no_whatsapp_result["citations"])
    no_group_candidates = answer(conn, "Show potential decisions in WhatsApp.", set())
    assert no_group_candidates["citations"] == []

    empty_allowlist_path = tmp_path / "whatsapp-empty-allowlist.json"
    export_source(conn, "whatsapp", empty_allowlist_path, selected_whatsapp_groups=set())
    assert json.loads(empty_allowlist_path.read_text(encoding="utf-8")) == []

    unsupported_result = answer(conn, "What is the chemical composition of the moon?", {selected_group})
    assert unsupported_result["citations"] == []
    assert "Insufficient evidence" in unsupported_result["answer"]

    # Retrieval returns cross-source evidence with citations; it does not combine
    # those records into a synthesized business conclusion.
    conn.close()
