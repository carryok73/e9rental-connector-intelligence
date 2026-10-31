from src.connector_intelligence.db import connect, upsert_record
from src.connector_intelligence.query import answer, retrieve


def _combined_question():
    return "What is the rental quotation amount and invoice payment status?"


def _add_quote_and_invoice(conn):
    upsert_record(
        conn, source="drive", source_id="quote-1", title="Vehicle rental quotation",
        content=("Vehicle rental quotation. Rental period: 12 October through 16 October "
                 "(4 days). Estimated total: INR 14,600."),
    )
    upsert_record(
        conn, source="drive", source_id="invoice-1", title="Invoice payment record",
        content=("Invoice number SYN-INV-1. Amount INR 14,600. Payment status: Pending. "
                 "Customer: Fictional Sample Account."),
    )


def test_combined_amount_and_payment_status_retrieves_both_facets_with_citations():
    conn = connect(":memory:")
    _add_quote_and_invoice(conn)

    result = answer(conn, _combined_question())

    assert [facet["status"] for facet in result["facets"]] == ["supported", "supported"]
    assert {facet["facet"] for facet in result["facets"]} == {
        "the rental quotation amount", "invoice payment status",
    }
    assert {item["source_id"] for item in result["citations"]} == {"quote-1", "invoice-1"}
    assert all(item["source"] == "drive" and item["match_type"] == "topic"
               for item in result["citations"])
    assert "does not generate a factual answer" in result["answer"]


def test_combined_facets_deduplicate_a_record_matching_both():
    conn = connect(":memory:")
    upsert_record(
        conn, source="drive", source_id="combined", title="Rental quotation invoice",
        content=("Rental quotation amount total INR 14600. Invoice payment status pending."),
    )

    result = answer(conn, _combined_question())

    assert len(result["facets"]) == 2
    assert all(facet["status"] == "supported" for facet in result["facets"])
    assert len(result["citations"]) == 1
    assert result["citations"][0]["source_id"] == "combined"


def test_phone_number_requires_a_phone_field_and_value():
    conn = connect(":memory:")
    upsert_record(
        conn, source="drive", source_id="customer-record", title="Customer verification",
        content="Customer: Fictional Sample. Document number: SYN-028.",
    )
    upsert_record(
        conn, source="drive", source_id="invoice-record", title="Invoice",
        content="Customer: Fictional Sample. Invoice number SYN-2041. Amount INR 500.",
    )

    result = answer(conn, "What is the customer's phone number?")

    assert result["citations"] == []
    assert result["facets"][0]["status"] == "insufficient_evidence"
    assert "Insufficient evidence" in result["answer"]


def test_phone_number_is_supported_only_when_a_labeled_value_exists():
    conn = connect(":memory:")
    upsert_record(
        conn, source="drive", source_id="with-phone", title="Contact sheet",
        content="Customer: Fictional Sample. Phone: +1 (555) 010-2040.",
    )
    result = answer(conn, "What is the customer's phone number?")
    assert [item["source_id"] for item in result["citations"]] == ["with-phone"]
    assert result["facets"][0]["status"] == "supported"


def test_partial_multi_facet_result_marks_missing_facet_explicitly():
    conn = connect(":memory:")
    upsert_record(
        conn, source="drive", source_id="quote-only", title="Vehicle rental quotation",
        content="Rental quotation amount INR 14,600. Rental period is four days.",
    )

    result = answer(conn, "What is the rental quotation amount and customer phone number?")

    assert [facet["status"] for facet in result["facets"]] == ["supported", "insufficient_evidence"]
    assert {item["source_id"] for item in result["citations"]} == {"quote-only"}
    assert "1 of 2 requested facets" in result["answer"]
    assert "phone number" in result["answer"]
    assert "no factual answer was generated" in result["answer"].lower()


def test_existing_single_facet_retrieval_still_returns_citations():
    conn = connect(":memory:")
    upsert_record(
        conn, source="drive", source_id="quote", title="Vehicle rental quotation",
        content="Rental quotation amount INR 14,600. Rental period is four days.",
    )
    result = answer(conn, "Find the vehicle rental quotation.")
    assert [item["source_id"] for item in result["citations"]] == ["quote"]
    assert result["facets"][0]["status"] == "supported"


def test_source_filter_and_parameterized_fts_query_remain_scoped():
    conn = connect(":memory:")
    upsert_record(conn, source="drive", source_id="drive-hit", title="Rental schedule",
                  content="Rental schedule approved.")
    upsert_record(conn, source="gmail", source_id="gmail-hit", title="Rental schedule",
                  content="Rental schedule approved.")

    results = retrieve(conn, 'Rental schedule" OR 1=1 --', source="drive")

    assert [row["source_id"] for row in results] == ["drive-hit"]

