import json
from datetime import date

from src.connector_intelligence.db import (
    connect, count_source_records, read_source_records, upsert_record,
)
from src.connector_intelligence.connectors.whatsapp import ingest_event, verify_token
from src.connector_intelligence.connectors.gmail import classify_email, attachment_flags, unanswered_inbound
from src.connector_intelligence.connectors.drive import classify_document
from src.connector_intelligence.query import answer
from src.connector_intelligence.query import (
    _expanded_query_terms, _query_terms, retrieve,
)
from src.connector_intelligence.exports import export_all, export_source


def test_whatsapp_filters_unselected_before_persisting():
    conn = connect(":memory:")
    payload = {"anything": "private"}
    mapper = lambda _: [
        {"group_id": "ok", "source_id": "1", "text": "allowed"},
        {"group_id": "no", "source_id": "2", "text": "secret"},
    ]
    assert ingest_event(conn, payload, {"ok"}, mapper) == 1
    assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    assert conn.execute("SELECT content FROM records").fetchone()[0] == "allowed"


def test_whatsapp_verification():
    assert verify_token("abc", "abc")
    assert not verify_token("abc", "")


def test_gmail_classification_and_attachment_reason():
    assert classify_email("Rental quote", "Please see pricing") == "sales"
    assert classify_email("Newsletter", "unsubscribe") == "marketing"
    assert classify_email("Question", "Hello") == "other"
    assert attachment_flags([{"filename": "invoice_april.pdf"}])[0]["reason"]


def test_unanswered_email_uses_actual_thread_replies():
    thread = [
        {"id": "in", "threadId": "thread-a", "internalDate": "1760659200000", "from": "customer@example.test", "_own_addresses": ["team@example.test"]},
        {"id": "reply", "threadId": "thread-a", "internalDate": "1760745600000", "from": "team@example.test", "_own_addresses": ["team@example.test"]},
        {"id": "old", "threadId": "thread-b", "internalDate": "1760572800000", "from": "other@example.test", "_own_addresses": ["team@example.test"]},
    ]
    result = unanswered_inbound(thread, date(2026, 10, 22), 5)
    assert [m["id"] for m in result] == ["old"]


def test_drive_category_has_reason():
    category, reason = classify_document("client-kyc.pdf", "identity verification")
    assert category == "verification" and reason


def test_query_citations_and_insufficient_evidence(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    upsert_record(conn, source="gmail", source_id="msg-1", title="Rental pricing", content="Customer requested a rental quote on Monday.")
    result = answer(conn, "Who requested rental pricing?")
    assert result["citations"][0]["source_id"] == "msg-1"
    assert "Insufficient evidence" in answer(conn, "What is the moon made of?")["answer"]


def test_query_searches_all_sources_and_exposes_source_context():
    conn = connect(":memory:")
    upsert_record(conn, source="whatsapp", source_id="wamid.exact-1", parent_id="group-approved",
                  occurred_at="1760000000", title="Rental schedule", content="Rental schedule confirmed",
                  metadata_json=json.dumps({"sender": "15550001"}))
    upsert_record(conn, source="gmail", source_id="gmail-message-exact", parent_id="thread-exact",
                  occurred_at="2026-10-20T12:34:56+00:00", title="Rental schedule",
                  content="Rental schedule attached", metadata_json=json.dumps({"sender": "customer@example.test"}))
    upsert_record(conn, source="drive", source_id="drive-file-exact", occurred_at="2026-10-19T00:00:00Z",
                  title="Rental schedule", content="Rental schedule for the customer",
                  metadata_json=json.dumps({"web_view_link": "https://drive.google.com/file/d/abc/view"}))

    result = answer(conn, "Rental schedule", {"group-approved"})
    citations = {item["source_type"]: item for item in result["citations"]}
    assert set(citations) == {"whatsapp", "gmail", "drive"}
    assert citations["whatsapp"]["source_id"] == "wamid.exact-1"
    assert citations["whatsapp"]["timestamp"] == "1760000000"
    assert citations["whatsapp"]["group_context"] == "group-approved"
    assert citations["whatsapp"]["sender"] == "15550001"
    assert citations["gmail"]["thread_id"] == "thread-exact"
    assert citations["gmail"]["sender"] == "customer@example.test"
    assert citations["gmail"]["timestamp"] == "2026-10-20T12:34:56+00:00"
    assert citations["drive"]["drive_link"] == "https://drive.google.com/file/d/abc/view"
    assert citations["drive"]["source_id"] == "drive-file-exact"
    assert all(item["excerpt"] for item in citations.values())
    assert "not a verified analytical answer" in result["answer"]


def test_query_requires_multiple_meaningful_terms_not_generic_single_overlap():
    conn = connect(":memory:")
    upsert_record(conn, source="gmail", source_id="generic-only", title="Rental question", content="Rental was discussed.")
    upsert_record(conn, source="drive", source_id="relevant", title="Rental safety inspection", content="Rental safety inspection completed.")
    result = answer(conn, "Rental safety inspection")
    assert [item["source_id"] for item in result["citations"]] == ["relevant"]


def test_query_handles_empty_punctuation_and_short_questions():
    conn = connect(":memory:")
    upsert_record(conn, source="gmail", source_id="one", title="Yes", content="Yes")
    for question in ("", "?!...", "a it", "the"):
        assert retrieve(conn, question) == []
        assert answer(conn, question)["citations"] == []
        assert "Insufficient evidence" in answer(conn, question)["answer"]


def test_query_ranking_is_deterministic_and_limit_applies_after_qualification():
    conn = connect(":memory:")
    for source, source_id in (("gmail", "z-last"), ("drive", "a-first"), ("whatsapp", "m-middle")):
        upsert_record(conn, source=source, source_id=source_id, title="Rental schedule", content="Rental schedule approved")
    first = retrieve(conn, "RENTAL schedule", limit=2)
    second = retrieve(conn, "rental schedule", limit=2)
    assert [row["source_id"] for row in first] == [row["source_id"] for row in second]
    assert len(first) == 2
    assert [row["source"] for row in first] == ["drive", "gmail"]


def test_query_missing_or_malformed_metadata_does_not_invent_context():
    conn = connect(":memory:")
    upsert_record(conn, source="gmail", source_id="bad-meta", title="Rental schedule", content="Rental schedule found",
                  parent_id=None, occurred_at="2026-10-22", metadata_json="not-json")
    citation = answer(conn, "Rental schedule")["citations"][0]
    assert citation["source_id"] == "bad-meta"
    assert citation["timestamp"] == "2026-10-22"
    assert citation["sender"] is None and citation["thread_id"] is None
    assert citation["group_context"] is None and citation["drive_link"] is None


def test_query_rejects_untrusted_drive_link():
    conn = connect(":memory:")
    upsert_record(conn, source="drive", source_id="link-test", title="Rental schedule", content="Rental schedule note",
                  metadata_json=json.dumps({"web_view_link": "https://example.test/file"}))
    assert answer(conn, "Rental schedule")["citations"][0]["drive_link"] is None


def test_source_count_query_behavior_remains_unchanged():
    conn = connect(":memory:")
    upsert_record(conn, source="gmail", source_id="m1")
    upsert_record(conn, source="gmail", source_id="m2")
    result = answer(conn, "How many Gmail records are stored?")
    assert result == {"answer": "There are 2 gmail records in the database.", "citations": [], "evidence": []}


def test_cross_source_topic_search_returns_qualified_matches_by_source():
    conn = connect(":memory:")
    for source, source_id in (("whatsapp", "w-1"), ("gmail", "g-1"), ("drive", "d-1")):
        upsert_record(conn, source=source, source_id=source_id, parent_id="selected-1" if source == "whatsapp" else None,
                      title="Delivery delays", content="The delivery delays affected the schedule.",
                      occurred_at=f"2026-10-2{len(source)}T00:00:00Z")
    result = answer(conn, "delivery delays", {"selected-1"})
    assert {item["source"] for item in result["citations"]} == {"whatsapp", "gmail", "drive"}
    assert all(item["match_type"] == "topic" for item in result["citations"])
    assert result["total_matching_records"] == 3
    assert "does not establish a verified" in result["answer"]


def test_exact_literal_match_is_case_and_punctuation_sensitive_across_sources():
    conn = connect(":memory:")
    for source, source_id in (("whatsapp", "wa-id"), ("gmail", "mail-id"), ("drive", "file-id")):
        upsert_record(conn, source=source, source_id=source_id,
                      parent_id="selected-1" if source == "whatsapp" else None,
                      title="Order#77 approved", content="Please reference Order#77.",
                      occurred_at="2026-10-22T10:11:12Z")
    exact = answer(conn, 'Find records containing "Order#77".', {"selected-1"})
    assert {item["source_id"] for item in exact["citations"]} == {"wa-id", "mail-id", "file-id"}
    assert all(item["match_type"] == "exact_literal" for item in exact["citations"])
    assert all(item["timestamp"] == "2026-10-22T10:11:12Z" for item in exact["citations"])
    assert answer(conn, 'Find records containing "order#77".', {"selected-1"})["total_matching_records"] == 0
    assert answer(conn, 'Find records containing "Order-77".', {"selected-1"})["total_matching_records"] == 0
    assert answer(conn, 'Find records containing "the".', {"selected-1"})["total_matching_records"] == 0


def test_exact_literal_and_topic_matches_have_distinct_nonduplicated_types():
    conn = connect(":memory:")
    upsert_record(conn, source="gmail", source_id="both", title="Order#77 delivery delays",
                  content="Order#77 caused delivery delays.")
    result = answer(conn, 'Find records about delivery delays containing "Order#77".')
    assert result["matching_counts"]["gmail"] == {"exact_literal": 1, "topic": 0, "total": 1}
    assert len(result["citations"]) == 1
    assert result["citations"][0]["match_type"] == "exact_literal"


def test_matching_counts_cover_all_qualified_rows_before_display_limit():
    conn = connect(":memory:")
    for index in range(10):
        source = ("whatsapp", "gmail", "drive")[index % 3]
        upsert_record(conn, source=source, source_id=f"{source}-{index}",
                      parent_id="selected-1" if source == "whatsapp" else None,
                      title="Delivery delays", content="Delivery delays recorded.")
    result = answer(conn, "How many records match delivery delays?", {"selected-1"})
    assert len(result["citations"]) == 8
    assert result["matching_counts"]["whatsapp"]["total"] == 4
    assert result["matching_counts"]["gmail"]["total"] == 3
    assert result["matching_counts"]["drive"]["total"] == 3
    assert result["total_matching_records"] == 10


def test_duplicate_source_ids_count_once_and_are_not_entity_counts():
    conn = connect(":memory:")
    upsert_record(conn, source="gmail", source_id="same-id", title="Delivery delays", content="Delivery delays noted.")
    upsert_record(conn, source="gmail", source_id="same-id", title="Delivery delays", content="Delivery delays updated.")
    result = answer(conn, "How many records match delivery delays?")
    assert result["matching_counts"]["gmail"]["total"] == 1
    assert result["total_matching_records"] == 1
    assert "not customer, order, or transaction counts" in result["answer"]


def _gmail_analytics_record(conn, source_id, *, inbound=True, unanswered=False,
                            sender="customer@example.test", reference="2026-10-22",
                            days=5, occurred_at="2026-10-17T00:00:00+00:00",
                            content="Rental inquiry"):
    metadata = {
        "is_inbound": inbound,
        "is_unanswered": unanswered,
        "sender": sender,
        "unanswered_reference_date": reference,
        "unanswered_days": days,
    }
    upsert_record(conn, source="gmail", source_id=source_id, parent_id=f"thread-{source_id}",
                  occurred_at=occurred_at, title=f"Mail {source_id}", content=content,
                  metadata_json=json.dumps(metadata))


def test_unanswered_email_exact_count_and_zero_count():
    conn = connect(":memory:")
    _gmail_analytics_record(conn, "old-in", unanswered=True)
    _gmail_analytics_record(conn, "answered-in", unanswered=False)
    result = answer(conn, "How many unanswered emails are there?")
    assert result["analytics"]["count"] == 1
    assert result["analytics"]["status"] == "complete_for_stored_records"
    assert "reference date 2026-10-22" in result["analytics"]["criteria"]
    assert "inclusive 5-day UTC calendar-date cutoff" in result["analytics"]["criteria"]

    zero = connect(":memory:")
    _gmail_analytics_record(zero, "all-answered", unanswered=False)
    result = answer(zero, "How many inbound emails are older than five days and unanswered?")
    assert result["analytics"]["count"] == 0
    assert "There are 0 unanswered" in result["answer"]


def test_unanswered_analytics_excludes_sent_and_answered_external_messages():
    conn = connect(":memory:")
    _gmail_analytics_record(conn, "unanswered-in", inbound=True, unanswered=True)
    _gmail_analytics_record(conn, "sent", inbound=False, unanswered=True, sender="team@example.test")
    _gmail_analytics_record(conn, "external-answered", inbound=True, unanswered=False,
                            sender="vendor@example.test")
    assert answer(conn, "How many unanswered emails?")["analytics"]["count"] == 1


def test_unanswered_analytics_deduplicates_by_gmail_source_id():
    conn = connect(":memory:")
    _gmail_analytics_record(conn, "same-message", unanswered=True)
    _gmail_analytics_record(conn, "same-message", unanswered=True, occurred_at="2026-10-16T00:00:00+00:00")
    assert conn.execute("SELECT COUNT(*) FROM records WHERE source='gmail'").fetchone()[0] == 1
    assert answer(conn, "How many unanswered emails?")["analytics"]["count"] == 1


def test_unanswered_analytics_fails_closed_on_missing_or_malformed_metadata():
    conn = connect(":memory:")
    _gmail_analytics_record(conn, "valid", unanswered=True)
    upsert_record(conn, source="gmail", source_id="missing-fields", title="Old email", content="Rental inquiry",
                  metadata_json=json.dumps({"is_unanswered": True}))
    result = answer(conn, "How many unanswered emails?")
    assert result["analytics"]["status"] == "incomplete"
    assert result["analytics"]["count"] is None
    assert "unavailable" in result["answer"].lower()

    malformed = connect(":memory:")
    upsert_record(malformed, source="gmail", source_id="bad-json", metadata_json="{broken")
    assert answer(malformed, "How many unanswered emails?")["analytics"]["status"] == "incomplete"


def test_unanswered_email_listing_returns_exact_record_citations():
    conn = connect(":memory:")
    _gmail_analytics_record(conn, "unanswered-1", unanswered=True, sender="customer@example.test")
    _gmail_analytics_record(conn, "answered-1", unanswered=False)
    result = answer(conn, "Show unanswered emails older than five days.")
    assert result["analytics"]["count"] == 1
    assert [item["source_id"] for item in result["citations"]] == ["unanswered-1"]
    assert result["citations"][0]["sender"] == "customer@example.test"
    assert result["citations"][0]["thread_id"] == "thread-unanswered-1"
    assert result["citations"][0]["timestamp"] == "2026-10-17T00:00:00+00:00"


def test_unanswered_email_query_requires_synced_records_and_does_not_fall_through():
    conn = connect(":memory:")
    result = answer(conn, "How many unanswered emails are there?")
    assert result["analytics"]["status"] == "unavailable"
    assert result["analytics"]["count"] is None
    assert "no stored Gmail records" in result["answer"]
    assert "sync completeness is not tracked" in result["analytics"]["coverage"]

    _gmail_analytics_record(conn, "unanswered", unanswered=True, content="Unrelated word index")
    unsupported = answer(conn, "How many unanswered WhatsApp messages?")
    assert "Unsupported analytical question" in unsupported["answer"]


def test_query_synonym_expansion_preserves_original_terms():
    original = _query_terms("What is the rental duration?")
    expanded = _expanded_query_terms(original)
    assert original == ["rental", "duration"]
    assert set(original).issubset(expanded)
    assert {"period", "days"}.issubset(expanded)
    assert set(_expanded_query_terms(["amount"])) == {"amount", "total", "price", "cost"}
    assert set(_expanded_query_terms(["budget"])) == {"budget", "expenditure", "allocation"}
    assert set(_expanded_query_terms(["phone"])) == {"phone", "telephone", "mobile"}


def test_duration_synonym_retrieves_quotation_but_alternative_alone_is_not_evidence():
    conn = connect(":memory:")
    upsert_record(conn, source="drive", source_id="quote", title="Rental quotation",
                  content="Rental period is four days.")
    upsert_record(conn, source="drive", source_id="unrelated", title="Delivery schedule",
                  content="The delivery period lasts fourteen days.")
    results = retrieve(conn, "What is the rental duration?", source="drive")
    assert [row["source_id"] for row in results] == ["quote"]


def test_budget_and_expenditure_synonyms_retrieve_only_relevant_workbook():
    conn = connect(":memory:")
    upsert_record(conn, source="drive", source_id="campaign", title="campaign.xlsx",
                  content="Worksheet: Campaign Budget\nChannel | Planned Expenditure | Actual Expenditure | Social Media | 50000 | 45000")
    upsert_record(conn, source="drive", source_id="unrelated", title="expense-policy.txt",
                  content="Expense allocation process for employee reimbursements.")
    results = retrieve(conn, "Show the marketing campaign budget.", source="drive")
    assert [row["source_id"] for row in results] == ["campaign"]


def test_export_serializes_modularly(tmp_path):
    conn = connect(":memory:")
    upsert_record(conn, source="drive", source_id="file-1", title="Doc", metadata_json=json.dumps({"reason": "test"}))
    path = tmp_path / "drive.json"
    export_source(conn, "drive", path)
    assert json.loads(path.read_text())[0]["metadata"]["reason"] == "test"


def test_whatsapp_reads_and_exports_follow_current_allowlist_and_keep_history(tmp_path):
    conn = connect(":memory:")
    upsert_record(conn, source="whatsapp", source_id="selected-wa", parent_id="group-current",
                  occurred_at="2026-10-22", title="WhatsApp message", content="selected content",
                  metadata_json=json.dumps({"sender": "sender-1"}))
    upsert_record(conn, source="whatsapp", source_id="removed-wa", parent_id="group-removed",
                  occurred_at="2026-10-21", title="WhatsApp message", content="historical content",
                  metadata_json=json.dumps({"sender": "sender-2"}))
    upsert_record(conn, source="gmail", source_id="mail-1", title="Mail", content="mail content")
    upsert_record(conn, source="drive", source_id="file-1", title="File", content="file content")

    displayed = read_source_records(conn, "whatsapp", {"group-current"})
    assert [row["source_id"] for row in displayed] == ["selected-wa"]
    assert count_source_records(conn, "whatsapp", {"group-current"}) == 1
    assert read_source_records(conn, "whatsapp", set()) == []
    assert count_source_records(conn, "whatsapp", set()) == 0

    wa_path = tmp_path / "whatsapp.json"
    export_source(conn, "whatsapp", wa_path, selected_whatsapp_groups={"group-current"})
    exported = json.loads(wa_path.read_text(encoding="utf-8"))
    assert [row["source_id"] for row in exported] == ["selected-wa"]
    assert exported[0]["metadata"] == {"sender": "sender-1"}
    assert set(exported[0]) == {
        "source_id", "parent_id", "occurred_at", "title", "content", "category", "metadata",
    }

    export_source(conn, "whatsapp", wa_path, selected_whatsapp_groups=set())
    assert json.loads(wa_path.read_text(encoding="utf-8")) == []

    export_all(conn, tmp_path / "all", selected_whatsapp_groups={"group-current"})
    assert [row["source_id"] for row in json.loads((tmp_path / "all" / "whatsapp_output.json").read_text())] == ["selected-wa"]
    assert [row["source_id"] for row in json.loads((tmp_path / "all" / "gmail_output.json").read_text())] == ["mail-1"]
    assert [row["source_id"] for row in json.loads((tmp_path / "all" / "drive_output.json").read_text())] == ["file-1"]

    export_all(conn, tmp_path / "empty", selected_whatsapp_groups=set())
    assert json.loads((tmp_path / "empty" / "whatsapp_output.json").read_text()) == []
    assert conn.execute("SELECT COUNT(*) FROM records WHERE source='whatsapp'").fetchone()[0] == 2


def test_whatsapp_source_count_query_uses_selected_group_only():
    conn = connect(":memory:")
    upsert_record(conn, source="whatsapp", source_id="wa-current", parent_id="group-current")
    upsert_record(conn, source="whatsapp", source_id="wa-removed", parent_id="group-removed")
    assert answer(conn, "How many WhatsApp records?", {"group-current"})["answer"].startswith(
        "There are 1 whatsapp records"
    )
    empty_scope = answer(conn, "How many WhatsApp records?", set())
    assert empty_scope["answer"].startswith("There are 0 whatsapp records")
    assert count_source_records(conn, "whatsapp", set()) == 0

