from io import BytesIO
import json

from openpyxl import Workbook

from src.connector_intelligence.connectors import drive
from src.connector_intelligence.connectors.drive_store import ingest_drive_file
from src.connector_intelligence.connectors.local_drive import import_local_folder
from src.connector_intelligence.db import connect, upsert_record
from src.connector_intelligence.exports import export_source
from src.connector_intelligence.query import answer


SAMPLE_ROOT = drive.Path(__file__).resolve().parents[1] / "data" / "imports" / "drive_sample"


def _locations(text, spans):
    return [{"start": text.index(fragment), "end": text.index(fragment) + len(fragment),
             "location": location} for fragment, location in spans]


def _record(conn, source_id, text, spans, *, title="Rental quotation"):
    upsert_record(conn, source="drive", source_id=source_id, title=title, content=text,
                  metadata_json=json.dumps({"source_locations": _locations(text, spans)}))


def test_local_import_persists_locations_and_exports_them_in_metadata(tmp_path):
    conn = connect(":memory:")
    try:
        report = import_local_folder(SAMPLE_ROOT, "drive-sample", conn)
        assert report["counts"]["imported"] == 5
        rows = conn.execute("SELECT metadata_json FROM records WHERE source='drive'").fetchall()
        assert len(rows) == 5
        for row in rows:
            metadata = json.loads(row[0])
            assert metadata["origin"] == "local_folder"
            assert metadata["source_locations"]
            assert str(SAMPLE_ROOT) not in row[0]

        output = tmp_path / "drive_output.json"
        export_source(conn, "drive", output)
        exported = json.loads(output.read_text(encoding="utf-8"))
        assert len(exported) == 5
        assert all(item["metadata"]["source_locations"] for item in exported)
        assert all(str(SAMPLE_ROOT) not in json.dumps(item) for item in exported)
    finally:
        conn.close()


class _Request:
    def __init__(self, content):
        self.content = content


class _Downloader:
    def __init__(self, output, request):
        self.output, self.request, self.done = output, request, False

    def next_chunk(self):
        if not self.done:
            self.output.write(self.request.content)
            self.done = True
        return None, self.done


class _Files:
    def get_media(self, **_kwargs):
        return _Request(b"Rental quotation amount: INR 500.")


class _Service:
    def files(self):
        return _Files()


def test_api_style_drive_ingestion_persists_detailed_locations(monkeypatch):
    monkeypatch.setattr(drive, "MediaIoBaseDownload", _Downloader)
    conn = connect(":memory:")
    try:
        result = ingest_drive_file(conn, _Service(), {
            "id": "api-drive-1", "name": "quote.txt", "mimeType": "text/plain",
        })
        assert result["status"] == "extracted"
        row = conn.execute("SELECT content,metadata_json FROM records WHERE source_id='api-drive-1'").fetchone()
        assert row[0] == "Rental quotation amount: INR 500."
        metadata = json.loads(row[1])
        assert metadata["source_locations"] == [
            {"start": 0, "end": len(row[0]), "location": {"line": 1}}
        ]
    finally:
        conn.close()


def test_pdf_evidence_citation_contains_only_overlapping_page():
    conn = connect(":memory:")
    text = "Rental quotation amount: INR 500.\nPayment status: Pending."
    _record(conn, "pdf-quote", text, [
        ("Rental quotation amount: INR 500.", {"page": 1}),
        ("Payment status: Pending.", {"page": 2}),
    ])
    citation = answer(conn, "Find rental quotation amount")['citations'][0]
    assert citation["source_locations"] == [{"page": 1}]


def test_xlsx_citation_contains_matching_worksheet_and_row():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Campaign Budget"
    sheet.append(["Campaign", "Planned Budget"])
    sheet.append(["Summer Promotion", 50000])
    data = BytesIO()
    workbook.save(data)
    workbook.close()
    extracted = drive.extract_text_with_locations(data.getvalue(), "budget.xlsx")
    location_data = [{key: item[key] for key in ("start", "end", "location")}
                     for item in extracted["locations"]]
    conn = connect(":memory:")
    upsert_record(conn, source="drive", source_id="xlsx-budget", title="Marketing workbook",
                  content=extracted["text"], metadata_json=json.dumps({"source_locations": location_data}))
    citation = answer(conn, "Show campaign budget")['citations'][0]
    assert {item["worksheet"] for item in citation["source_locations"]} == {"Campaign Budget"}
    assert any(item.get("row") == 1 for item in citation["source_locations"])


def test_txt_and_markdown_citation_maps_to_matching_line():
    conn = connect(":memory:")
    text = "Customer verification checklist\nIdentity document: verified\nAddress proof: pending\n"
    _record(conn, "txt-check", text, [
        ("Customer verification checklist\n", {"line": 1}),
        ("Identity document: verified\n", {"line": 2}),
        ("Address proof: pending\n", {"line": 3}),
    ], title="Verification checklist")
    citation = answer(conn, "Find customer verification checklist")['citations'][0]
    assert citation["source_locations"] == [{"line": 1}]


def test_docx_paragraph_and_table_citations_preserve_location_types():
    conn = connect(":memory:")
    text = "Meeting decisions\nRental process approved\nOwner | Task\nTeam A | Update quotation"
    _record(conn, "docx-notes", text, [
        ("Meeting decisions", {"paragraph": 1, "section": "Operations"}),
        ("Rental process approved", {"paragraph": 2, "section": "Operations"}),
        ("Owner", {"table": 1, "row": 1, "cell": 1}),
        ("Task", {"table": 1, "row": 1, "cell": 2}),
        ("Team A", {"table": 1, "row": 2, "cell": 1}),
        ("Update quotation", {"table": 1, "row": 2, "cell": 2}),
    ], title="Operations meeting notes")
    citation = answer(conn, "Find rental process decisions")['citations'][0]
    assert citation["source_locations"] == [
        {"paragraph": 1, "section": "Operations"},
        {"paragraph": 2, "section": "Operations"},
    ]

    table_citation = answer(conn, "Find update quotation")['citations'][0]
    assert table_citation["source_locations"] == [{"table": 1, "row": 2, "cell": 2}]


def test_missing_or_invalid_location_metadata_falls_back_to_record_citation():
    conn = connect(":memory:")
    text = "Rental quotation amount INR 500."
    upsert_record(conn, source="drive", source_id="old-record", title="Rental quotation",
                  content=text, metadata_json="not-json")
    upsert_record(conn, source="drive", source_id="bad-spans", title="Rental quotation",
                  content=text, metadata_json=json.dumps({"source_locations": [
                      {"start": -5, "end": 999, "location": {"page": 900}},
                      {"start": 0, "end": len(text), "location": "not-an-object"},
                      {"start": 0, "end": len(text),
                       "location": {"page": 1, "path": "C:\\Users\\private\\quote.pdf"}},
                  ]}))
    result = answer(conn, "Find rental quotation amount")
    assert {item["source_id"] for item in result["citations"]} == {"old-record", "bad-spans"}
    citations = {item["source_id"]: item for item in result["citations"]}
    assert citations["old-record"]["source_locations"] == []
    assert citations["bad-spans"]["source_locations"] == [{"page": 1}]
    assert "path" not in json.dumps(citations["bad-spans"]["source_locations"])


def test_multi_facet_citations_keep_distinct_locations_and_deduplicate_record():
    conn = connect(":memory:")
    text = "Rental quotation amount INR 500.\nInvoice payment status: Pending."
    _record(conn, "multi", text, [
        ("Rental quotation amount INR 500.", {"page": 1}),
        ("Invoice payment status: Pending.", {"page": 2}),
    ], title="Rental quotation and invoice")
    result = answer(conn, "What is the rental quotation amount and invoice payment status?")
    assert len(result["citations"]) == 1
    assert result["citations"][0]["source_id"] == "multi"
    assert result["citations"][0]["source_locations"] == [{"page": 1}, {"page": 2}]
    facet_locations = [facet["citations"][0]["source_locations"] for facet in result["facets"]]
    assert facet_locations == [[{"page": 1}], [{"page": 2}]]
