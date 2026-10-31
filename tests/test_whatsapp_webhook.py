import hashlib
import hmac
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.connector_intelligence import api
from src.connector_intelligence.connectors.whatsapp import (
    ingest_cloud_api_event,
    normalize_selected_messages,
    verify_signature,
    verify_token,
)
from src.connector_intelligence.db import connect
from src.connector_intelligence.db import upsert_record
from src.connector_intelligence.query import answer


SECRET = "unit-test-secret"


def signature(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def message(mid="wamid.1", group="group-1", text="Please send the quote."):
    return {"id": mid, "from": "15550001", "timestamp": "1760000000", "type": "text",
            "text": {"body": text}, "organizer_group_ref": group}


def payload(*messages):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages",
            "value": {"messages": list(messages), "statuses": [{"id": "status-only"}]}}]}]}


def test_verification_echoes_exact_challenge_and_rejects_bad_token():
    client = TestClient(api.app)
    monkeypatch_settings = SimpleNamespace(whatsapp_verify_token="approved-token")
    original = api.Settings
    api.Settings = lambda: monkeypatch_settings
    try:
        good = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "approved-token", "hub.challenge": "challenge-abc"})
        bad = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "challenge-abc"})
        assert good.status_code == 200 and good.text == "challenge-abc"
        assert bad.status_code == 403
    finally:
        api.Settings = original
    assert verify_token("same", "same") and not verify_token("same", "")


def test_meta_signature_validation_accepts_only_matching_hmac():
    body = b'{"entry":[]}'
    assert verify_signature(body, signature(body), SECRET)
    assert not verify_signature(body, "sha256=" + "0" * 64, SECRET)
    assert not verify_signature(body, signature(body), "")


def test_filtering_happens_before_interpreting_and_missing_group_path_fails_closed(monkeypatch):
    import src.connector_intelligence.connectors.whatsapp as connector

    def should_not_interpret(_):
        raise AssertionError("unselected content reached interpretation")

    monkeypatch.setattr(connector, "interpret_message", should_not_interpret)
    assert normalize_selected_messages(payload(message(group="unselected")), {"selected"}, "organizer_group_ref") == []
    assert normalize_selected_messages(payload(message()), {"group-1"}, "") == []


def test_direct_missing_and_malformed_group_ids_fail_closed():
    # The field name is a test-only configured path, not an organizer schema claim.
    direct = message()
    direct.pop("organizer_group_ref")
    malformed = [message(group=value) for value in (None, {}, [], True, "", "   ")]
    events = payload(direct, *malformed)
    assert normalize_selected_messages(events, {"group-1", "True", ""}, "organizer_group_ref") == []


def test_selected_text_extracts_metadata_and_local_interpretation():
    result = normalize_selected_messages(payload(message()), {"group-1"}, "organizer_group_ref")
    assert len(result) == 1
    item = result[0]
    assert (item["group_id"], item["source_id"], item["text"], item["timestamp"]) == (
        "group-1", "wamid.1", "Please send the quote.", "1760000000")
    assert item["metadata"]["sender"] == "15550001"
    assert item["metadata"]["interpretation"]["action_items"] == ["Please send the quote."]


def test_status_unsupported_malformed_and_duplicate_delivery(tmp_path):
    conn = connect(str(tmp_path / "test.db"))
    assert ingest_cloud_api_event(conn, {"entry": [{"changes": [{"value": {"statuses": [{"id": "x"}]}}]}]}, {"group-1"}, "organizer_group_ref") == 0
    assert ingest_cloud_api_event(conn, {"unexpected": []}, {"group-1"}, "organizer_group_ref") == 0
    event = payload(message())
    assert ingest_cloud_api_event(conn, event, {"group-1"}, "organizer_group_ref") == 1
    assert ingest_cloud_api_event(conn, event, {"group-1"}, "organizer_group_ref") == 1
    assert conn.execute("SELECT COUNT(*) FROM records WHERE source='whatsapp'").fetchone()[0] == 1
    conn.close()


def test_post_requires_signature_filters_then_exports(tmp_path, caplog):
    client = TestClient(api.app)
    settings = SimpleNamespace(
        whatsapp_app_secret=SECRET,
        whatsapp_selected_groups="group-1",
        whatsapp_group_id_path="organizer_group_ref",
        database_path=str(tmp_path / "db.sqlite"),
        output_dir=str(tmp_path / "out"),
        ensure_dirs=lambda: None,
        selected_whatsapp_groups=lambda: {"group-1"},
    )
    with connect(settings.database_path) as conn:
        upsert_record(conn, source="whatsapp", source_id="previously-stored",
                      parent_id="group-removed", title="Historical message",
                      content="historical removed-group content")
    original = api.Settings
    api.Settings = lambda: settings
    try:
        raw = json.dumps(payload(message(), message("wamid.2", "not-selected", "private"))).encode()
        denied = client.post("/webhooks/whatsapp", content=raw)
        invalid = client.post("/webhooks/whatsapp", content=b"not-json", headers={"X-Hub-Signature-256": "sha256=" + "0" * 64})
        accepted = client.post("/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": signature(raw)})
        malformed = b"{" 
        malformed_response = client.post("/webhooks/whatsapp", content=malformed, headers={"X-Hub-Signature-256": signature(malformed)})
        assert denied.status_code == 403
        assert invalid.status_code == 403  # signature check runs before JSON parsing
        assert accepted.status_code == 200 and accepted.json()["stored_messages"] == 1
        assert malformed_response.status_code == 400
        output = json.loads((tmp_path / "out" / "whatsapp_output.json").read_text(encoding="utf-8"))
        assert len(output) == 1 and output[0]["source_id"] == "wamid.1"
        assert "private" not in json.dumps(output)
        assert "historical removed-group content" not in json.dumps(output)
        assert "private" not in caplog.text
        with connect(str(tmp_path / "db.sqlite")) as conn:
            rows = conn.execute("SELECT source_id, content FROM records WHERE source='whatsapp'").fetchall()
            assert {(row["source_id"], row["content"]) for row in rows} == {
                ("wamid.1", "Please send the quote."),
                ("previously-stored", "historical removed-group content"),
            }
            assert conn.execute("SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH 'private'").fetchone()[0] == 0
    finally:
        api.Settings = original


def _stored_whatsapp(conn, source_id, group_id, text, interpretation, *, metadata_json=None,
                     timestamp="1760000000", sender="15550001"):
    metadata = {"sender": sender, "message_type": "text", "interpretation": interpretation}
    upsert_record(conn, source="whatsapp", source_id=source_id, parent_id=group_id,
                  occurred_at=timestamp, title="WhatsApp message", content=text,
                  metadata_json=metadata_json if metadata_json is not None else json.dumps(metadata))


def test_whatsapp_decision_request_and_action_candidates_return_original_citations():
    conn = connect(":memory:")
    interpretation = {
        "summary": "We approved delivery on Friday.",
        "decisions": ["We approved delivery on Friday."],
        "requests_and_commitments": ["Please send the delivery schedule."],
        "action_items": ["Please send the delivery schedule."],
        "method": "local keyword and sentence heuristics; candidates require human review",
    }
    _stored_whatsapp(conn, "wamid.exact-1", "selected-1",
                     "We approved delivery on Friday. Please send the delivery schedule.", interpretation,
                     timestamp="1760000123", sender="15551234")

    decision = answer(conn, "Show potential decisions in WhatsApp.", {"selected-1"})
    commitment = answer(conn, "Show potential commitments in WhatsApp.", {"selected-1"})
    actions = answer(conn, "Show candidate action items in WhatsApp.", {"selected-1"})
    for result, expected in ((decision, "potential decision candidates"),
                             (commitment, "request/commitment candidates"),
                             (actions, "candidate action items")):
        assert len(result["citations"]) == 1
        citation = result["citations"][0]
        assert citation["source_id"] == "wamid.exact-1"
        assert citation["timestamp"] == "1760000123"
        assert citation["sender"] == "15551234"
        assert citation["group_context"] == "selected-1"
        assert citation["candidate_type"] == expected
        assert "heuristic candidate" in citation["interpretation_label"].lower()
        assert result["evidence"][0]["content"] == "We approved delivery on Friday. Please send the delivery schedule."
    assert "request/commitment candidates" in commitment["answer"]
    assert "not confirmed" in decision["answer"]


def test_whatsapp_candidate_queries_skip_empty_missing_and_malformed_metadata():
    conn = connect(":memory:")
    _stored_whatsapp(conn, "empty", "selected-1", "Nothing happened.", {"decisions": [], "action_items": [], "requests_and_commitments": []})
    _stored_whatsapp(conn, "missing", "selected-1", "Approved delivery.", {})
    _stored_whatsapp(conn, "malformed", "selected-1", "Approved delivery.", {}, metadata_json="{broken")
    _stored_whatsapp(conn, "wrong-type", "selected-1", "Approved delivery.", {}, metadata_json=json.dumps({"interpretation": {"decisions": "approved"}}))
    result = answer(conn, "Show potential decisions in WhatsApp.", {"selected-1"})
    assert result["citations"] == []
    assert "Skipped 3 record(s)" in result["answer"]


def test_whatsapp_queries_are_source_and_allowlist_scoped():
    conn = connect(":memory:")
    interpretation = {"decisions": ["Delivery was approved."], "action_items": [], "requests_and_commitments": []}
    _stored_whatsapp(conn, "selected-record", "selected-1", "Delivery delays were approved.", interpretation)
    _stored_whatsapp(conn, "unselected-record", "unselected-2", "Delivery delays were approved.", interpretation)
    upsert_record(conn, source="gmail", source_id="gmail-same", title="WhatsApp message",
                  content="Delivery delays were approved.", metadata_json=json.dumps({"interpretation": interpretation}))
    upsert_record(conn, source="drive", source_id="drive-same", title="WhatsApp message",
                  content="Delivery delays were approved.", metadata_json=json.dumps({"interpretation": interpretation}))

    candidates = answer(conn, "Show potential decisions in WhatsApp.", {"selected-1"})
    topic = answer(conn, "Find WhatsApp messages about delivery delays.", {"selected-1"})
    specific = answer(conn, "Show potential decisions in WhatsApp group selected-1.", {"selected-1"})
    rejected = answer(conn, "Show potential decisions in WhatsApp group unselected-2.", {"selected-1"})
    assert [item["source_id"] for item in candidates["citations"]] == ["selected-record"]
    assert [item["source_id"] for item in topic["citations"]] == ["selected-record"]
    assert [item["source_id"] for item in specific["citations"]] == ["selected-record"]
    assert rejected["citations"] == []
    assert "not an exact member" in rejected["answer"]


def test_whatsapp_candidate_queries_fail_closed_without_selected_groups():
    conn = connect(":memory:")
    _stored_whatsapp(conn, "stored", "some-group", "Decision approved.", {"decisions": ["Decision approved."]})
    result = answer(conn, "Show potential decisions in WhatsApp.")
    assert result["citations"] == []
    assert "no WhatsApp groups are currently selected" in result["answer"]


def test_candidate_decision_evidence_requires_topic_and_candidate_record():
    conn = connect(":memory:")
    _stored_whatsapp(conn, "decision-match", "selected-1", "Delivery decision approved.",
                     {"decisions": ["Delivery decision approved."]})
    _stored_whatsapp(conn, "no-decision", "selected-1", "Delivery decision discussed.", {"decisions": []})
    upsert_record(conn, source="gmail", source_id="other-source", title="Delivery decision",
                  content="Delivery decision approved.")
    result = answer(conn, "Find WhatsApp evidence about the delivery decision.", {"selected-1"})
    assert [item["source_id"] for item in result["citations"]] == ["decision-match"]
    assert "matching candidate evidence" in result["answer"].lower()


def test_query_api_passes_current_selected_group_allowlist(tmp_path):
    client = TestClient(api.app)
    database = str(tmp_path / "query.sqlite")
    with connect(database) as conn:
        interpretation = {"decisions": ["Delivery approved."], "action_items": [], "requests_and_commitments": []}
        _stored_whatsapp(conn, "selected", "selected-1", "Delivery approved.", interpretation)
        _stored_whatsapp(conn, "not-selected", "unselected-2", "Delivery approved.", interpretation)
    settings = SimpleNamespace(
        database_path=database,
        selected_whatsapp_groups=lambda: {"selected-1"},
    )
    original = api.Settings
    api.Settings = lambda: settings
    try:
        response = client.post("/query", json={"question": "Show potential decisions in WhatsApp."})
        assert response.status_code == 200
        assert [item["source_id"] for item in response.json()["citations"]] == ["selected"]
    finally:
        api.Settings = original


def test_cross_source_search_exact_and_counts_obey_whatsapp_allowlist():
    conn = connect(":memory:")
    _stored_whatsapp(conn, "wa-selected", "selected-1", "Delivery delays reference Order#77.", {})
    _stored_whatsapp(conn, "wa-unselected", "unselected-2", "Delivery delays reference Order#77.", {})
    upsert_record(conn, source="gmail", source_id="mail", title="Delivery delays",
                  content="Delivery delays reference Order#77.")
    upsert_record(conn, source="drive", source_id="file", title="Delivery delays",
                  content="Delivery delays reference Order#77.")

    topic = answer(conn, "delivery delays", {"selected-1"})
    exact = answer(conn, 'Find records containing "Order#77".', {"selected-1"})
    counted = answer(conn, "How many records match delivery delays?", {"selected-1"})
    for result in (topic, exact, counted):
        ids = {item["source_id"] for item in result["citations"]}
        assert "wa-selected" in ids
        assert "wa-unselected" not in ids
        assert {"mail", "file"}.issubset(ids)

    no_groups = answer(conn, "delivery delays", set())
    no_groups_exact = answer(conn, 'Find records containing "Order#77".', set())
    no_groups_count = answer(conn, "How many records match delivery delays?", set())
    for result in (no_groups, no_groups_exact, no_groups_count):
        ids = {item["source_id"] for item in result["citations"]}
        assert "wa-selected" not in ids and "wa-unselected" not in ids
        assert {"mail", "file"}.issubset(ids)
        assert result["matching_counts"]["whatsapp"]["total"] == 0
        assert result["whatsapp_scope"]["status"] == "excluded_no_groups_selected"

    assert answer(conn, "How many WhatsApp records?", {"selected-1"})["answer"].startswith("There are 1 whatsapp records")
    assert answer(conn, "How many WhatsApp records?", set())["answer"].startswith("There are 0 whatsapp records")


def test_api_cross_source_query_uses_allowlist_and_preserves_other_sources(tmp_path):
    client = TestClient(api.app)
    database = str(tmp_path / "cross-source.sqlite")
    with connect(database) as conn:
        _stored_whatsapp(conn, "wa-selected", "selected-1", "Order#88 is confirmed.", {})
        _stored_whatsapp(conn, "wa-unselected", "unselected-2", "Order#88 is confirmed.", {})
        upsert_record(conn, source="gmail", source_id="mail", title="Order#88", content="Order#88 is confirmed.")
        upsert_record(conn, source="drive", source_id="file", title="Order#88", content="Order#88 is confirmed.")
    settings = SimpleNamespace(database_path=database, selected_whatsapp_groups=lambda: {"selected-1"})
    original = api.Settings
    api.Settings = lambda: settings
    try:
        response = client.post("/query", json={"question": 'Find records containing "Order#88".'})
        assert response.status_code == 200
        ids = {item["source_id"] for item in response.json()["citations"]}
        assert ids == {"wa-selected", "mail", "file"}
    finally:
        api.Settings = original
