import base64
from datetime import date, datetime, timezone
import json

import pytest

from src.connector_intelligence.connectors.gmail import (
    GmailConnectorError,
    attachment_flags,
    classify_email,
    fetch_gmail_messages,
    normalize_gmail_message,
    sync_gmail,
    unanswered_inbound,
)
from src.connector_intelligence.db import connect


def encoded(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def raw_message(message_id, thread_id, *, sender="customer@example.test", subject="Question", body="Hello",
                internal_date="1760659200000", parts=None):
    return {
        "id": message_id,
        "threadId": thread_id,
        "internalDate": internal_date,
        "payload": {
            "mimeType": "multipart/mixed" if parts else "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "team@example.test"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Fri, 17 Oct 2025 00:00:00 +0000"},
                {"name": "Message-ID", "value": f"<{message_id}@example.test>"},
            ],
            "body": {"data": encoded(body)} if not parts else {},
            "parts": parts or [],
        },
    }


class Request:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class MockGmailService:
    def __init__(self, pages, threads, profile="team@example.test", error_at=None):
        self.pages = pages
        self.threads_data = threads
        self.profile = profile
        self.error_at = error_at
        self.list_calls = []
        self.thread_calls = []
        self.message_calls = []

    def users(self):
        return self

    def messages(self):
        return self

    def threads(self):
        return self

    def getProfile(self, **kwargs):
        return Request({"emailAddress": self.profile})

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.error_at == "list":
            return Request(error=RuntimeError("sensitive response body"))
        page = self.pages[1 if kwargs.get("pageToken") else 0]
        return Request(page)

    def get(self, **kwargs):
        if "id" in kwargs and kwargs["id"] in self.threads_data:
            self.thread_calls.append(kwargs)
            if self.error_at == "thread":
                return Request(error=RuntimeError("sensitive response body"))
            return Request(self.threads_data[kwargs["id"]])
        self.message_calls.append(kwargs)
        return Request({})


def test_fetch_gmail_messages_paginates_and_fetches_each_thread_once():
    first = raw_message("m1", "t1")
    second = raw_message("m2", "t1", sender="team@example.test", internal_date="1760745600000")
    third = raw_message("m3", "t2", subject="Rental quote", body="Please send pricing")
    service = MockGmailService(
        pages=[
            {"messages": [{"id": "m1", "threadId": "t1"}], "nextPageToken": "next"},
            {"messages": [{"id": "m2", "threadId": "t1"}, {"id": "m3", "threadId": "t2"}]},
        ],
        threads={"t1": {"messages": [first, second]}, "t2": {"messages": [third]}},
    )
    messages = fetch_gmail_messages(service, page_size=2)
    assert {message["id"] for message in messages} == {"m1", "m2", "m3"}
    assert [call.get("pageToken") for call in service.list_calls] == [None, "next"]
    assert len(service.thread_calls) == 2
    assert service.list_calls[0]["userId"] == "me"
    assert service.list_calls[0]["includeSpamTrash"] is False


def test_normalization_extracts_fields_body_and_attachment_metadata():
    message = raw_message(
        "m1", "t1", subject="Rental quote", body="",
        parts=[
            {"mimeType": "text/plain", "body": {"data": encoded("Please review")}},
            {"mimeType": "application/pdf", "filename": "Invoice_42.pdf", "body": {"attachmentId": "att-1", "size": 120}},
        ],
    )
    normalized = normalize_gmail_message(message)
    assert normalized["from"] == "customer@example.test"
    assert normalized["to"] == ["team@example.test"]
    assert normalized["threadId"] == "t1"
    assert normalized["body"] == "Please review"
    assert normalized["attachments"][0]["attachment_id"] == "att-1"
    assert normalized["attachment_flags"][0]["category"] == "invoice"
    assert normalized["category"] == "sales"


def test_classification_always_returns_one_explainable_category():
    assert classify_email("Newsletter", "Unsubscribe here") == "marketing"
    assert classify_email("Rental pricing request", "Please send a quote") == "sales"
    assert classify_email("Question", "Hello") == "other"


def test_unanswered_cutoff_includes_exact_five_day_boundary_only():
    def message_at(message_id, day):
        timestamp = int(datetime(2026, 10, day, tzinfo=timezone.utc).timestamp() * 1000)
        return {
            "id": message_id,
            "from": "customer@example.test",
            "internalDate": str(timestamp),
            "_own_addresses": ["team@example.test"],
        }

    messages = [message_at("boundary", 17), message_at("too_recent", 18)]
    assert [message["id"] for message in unanswered_inbound(messages, date(2026, 10, 22), days=5)] == ["boundary"]


def _thread_message(message_id, sender, day, thread_id="thread-1"):
    timestamp = int(datetime(2026, 10, day, 12, tzinfo=timezone.utc).timestamp() * 1000)
    return {
        "id": message_id,
        "threadId": thread_id,
        "from": sender,
        "internalDate": str(timestamp),
        "_own_addresses": ["team@example.test"],
    }


def test_unanswered_one_inbound_with_no_owner_reply():
    inbound = _thread_message("inbound", "customer@example.test", 16)
    assert [message["id"] for message in unanswered_inbound([inbound], date(2026, 10, 22))] == ["inbound"]


def test_unanswered_single_inbound_followed_by_owner_reply():
    messages = [
        _thread_message("inbound", "customer@example.test", 16),
        _thread_message("owner-reply", "team@example.test", 17),
    ]
    assert unanswered_inbound(messages, date(2026, 10, 22)) == []


def test_unanswered_two_inbounds_followed_by_one_owner_reply():
    messages = [
        _thread_message("older-inbound", "customer@example.test", 15),
        _thread_message("newer-inbound", "customer@example.test", 16),
        _thread_message("owner-reply", "team@example.test", 17),
    ]
    assert unanswered_inbound(messages, date(2026, 10, 22)) == []


def test_owner_reply_before_inbound_does_not_answer_it():
    messages = [
        _thread_message("owner-reply", "team@example.test", 15),
        _thread_message("inbound", "customer@example.test", 16),
    ]
    assert [message["id"] for message in unanswered_inbound(messages, date(2026, 10, 22))] == ["inbound"]


def test_external_reply_does_not_count_as_owner_reply():
    messages = [
        _thread_message("inbound", "customer@example.test", 15),
        _thread_message("external-reply", "another@example.test", 17),
    ]
    assert [message["id"] for message in unanswered_inbound(messages, date(2026, 10, 22))] == ["inbound", "external-reply"]


def test_five_day_boundary_is_inclusive_and_recent_message_is_excluded():
    messages = [
        _thread_message("boundary", "customer@example.test", 17),
        _thread_message("too-recent", "customer@example.test", 18),
    ]
    assert [message["id"] for message in unanswered_inbound(messages, date(2026, 10, 22), days=5)] == ["boundary"]


def test_reply_in_another_thread_does_not_answer_inbound():
    messages = [
        _thread_message("inbound", "customer@example.test", 15, thread_id="thread-a"),
        _thread_message("owner-reply", "team@example.test", 17, thread_id="thread-b"),
    ]
    assert [message["id"] for message in unanswered_inbound(messages, date(2026, 10, 22))] == ["inbound"]


def test_missing_or_malformed_timestamps_are_skipped_safely():
    inbound = _thread_message("valid-inbound", "customer@example.test", 15)
    malformed_reply = {
        "id": "malformed-reply", "threadId": "thread-1", "from": "team@example.test",
        "internalDate": "not-a-timestamp", "date": "also not a date",
        "_own_addresses": ["team@example.test"],
    }
    missing_timestamp = {"id": "missing", "threadId": "thread-1", "from": "customer@example.test",
                         "_own_addresses": ["team@example.test"]}
    assert [message["id"] for message in unanswered_inbound(
        [inbound, malformed_reply, missing_timestamp], date(2026, 10, 22))] == ["valid-inbound"]


def test_attachment_rules_flag_critical_terms_without_substring_po_false_positive():
    flags = attachment_flags([
        {"filename": "invoice_april.pdf"},
        {"filename": "PO-4821.pdf"},
        {"filename": "quotation.docx"},
        {"filename": "signed-contract.pdf"},
        {"filename": "customer-KYC.pdf"},
        {"filename": "report.pdf"},
    ])
    assert [item["category"] for item in flags] == ["invoice", "purchase_order", "quotation", "contract", "verification"]
    assert all(item["reason"] for item in flags)


def test_sync_checks_mailbox_marks_unanswered_and_deduplicates(tmp_path):
    inbound = raw_message("in", "t1", internal_date="1760572800000", subject="Rental request")
    owner_reply = raw_message("reply", "t2", sender="team@example.test", internal_date="1760659200000")
    service = MockGmailService(
        pages=[{"messages": [{"id": "in", "threadId": "t1"}, {"id": "reply", "threadId": "t2"}]}],
        threads={"t1": {"messages": [inbound]}, "t2": {"messages": [owner_reply]}},
    )
    conn = connect(":memory:")
    first = sync_gmail(conn, service, mailbox_owner="team@example.test", reference_date=date(2026, 10, 22), output_dir=tmp_path)
    second = sync_gmail(conn, service, mailbox_owner="team@example.test", reference_date=date(2026, 10, 22), output_dir=tmp_path)
    assert first["messages_fetched"] == 2
    assert second["messages_fetched"] == 2
    assert conn.execute("SELECT COUNT(*) FROM records WHERE source='gmail'").fetchone()[0] == 2
    row = conn.execute("SELECT metadata_json FROM records WHERE source_id='in'").fetchone()
    row_metadata = json.loads(row["metadata_json"])
    assert row_metadata["is_unanswered"] is True
    assert row_metadata["unanswered_reference_date"] == "2026-10-22"
    assert row_metadata["unanswered_days"] == 5
    reply = conn.execute("SELECT category,metadata_json FROM records WHERE source_id='reply'").fetchone()
    assert json.loads(reply["metadata_json"])["is_inbound"] is False
    exported = json.loads((tmp_path / "gmail_output.json").read_text(encoding="utf-8"))
    assert len(exported) == 2
    assert exported[0]["metadata"]["sender"]


def test_sync_refuses_wrong_mailbox_before_listing_messages():
    service = MockGmailService(pages=[{"messages": []}], threads={}, profile="personal@example.test")
    with pytest.raises(GmailConnectorError, match="does not match"):
        sync_gmail(connect(":memory:"), service, mailbox_owner="sandbox@example.test", reference_date=date(2026, 10, 22))
    assert service.list_calls == []


def test_api_errors_are_reported_without_response_body():
    service = MockGmailService(pages=[{}], threads={}, error_at="list")
    with pytest.raises(GmailConnectorError, match="messages.list") as error:
        fetch_gmail_messages(service)
    assert "sensitive response body" not in str(error.value)
