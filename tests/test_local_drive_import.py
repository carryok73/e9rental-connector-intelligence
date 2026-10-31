from pathlib import Path

import pytest

from src.connector_intelligence.connectors import local_drive
from src.connector_intelligence.connectors.local_drive import (
    LocalDriveScanError,
    extract_scanned_file,
    import_local_folder,
    scan_local_folder,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_ROOT = PROJECT_ROOT / "data" / "imports" / "drive_sample"
EXPECTED_SAMPLE_FILES = {
    "document_001.pdf",
    "document_002.docx",
    "document_003.xlsx",
    "document_004.txt",
    "document_005.csv",
}


def test_discovers_all_five_synthetic_sample_documents():
    files = scan_local_folder(SAMPLE_ROOT, "drive-sample")
    assert {item["filename"] for item in files} == EXPECTED_SAMPLE_FILES
    assert len(files) == 5
    assert all(item["file_status"] == "ready" for item in files)
    assert all(item["file_size"] > 0 for item in files)


def test_discovers_files_in_nested_directories(tmp_path):
    nested = tmp_path / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "notes.md").write_text("synthetic", encoding="utf-8")
    files = scan_local_folder(tmp_path, "root-a")
    assert len(files) == 1
    assert files[0]["relative_path"] == "one/two/notes.md"
    assert files[0]["file_status"] == "ready"


def test_reports_unsupported_extensions(tmp_path):
    unsupported = tmp_path / "archive.zip"
    unsupported.write_bytes(b"not inspected")
    files = scan_local_folder(tmp_path, "root-a")
    assert len(files) == 1
    assert files[0]["extension"] == ".zip"
    assert files[0]["file_status"] == "unsupported"
    assert files[0]["file_size"] == len(b"not inspected")


def test_empty_directory_returns_no_files(tmp_path):
    assert scan_local_folder(tmp_path, "empty") == []


def test_missing_directory_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="does not exist"):
        scan_local_folder(tmp_path / "missing", "root-a")


def test_file_as_root_is_rejected(tmp_path):
    file_path = tmp_path / "not-a-directory.txt"
    file_path.write_text("sample", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        scan_local_folder(file_path, "root-a")


def test_ids_are_stable_across_repeated_scans(tmp_path):
    (tmp_path / "record.csv").write_text("a,b", encoding="utf-8")
    first = scan_local_folder(tmp_path, "root-a")
    second = scan_local_folder(tmp_path, "root-a")
    assert first[0]["source_id"] == second[0]["source_id"]


def test_ids_use_root_label_and_relative_path_not_absolute_root(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "nested").mkdir()
    (second_root / "nested").mkdir()
    (first_root / "nested" / "record.csv").write_text("a,b", encoding="utf-8")
    (second_root / "nested" / "record.csv").write_text("a,b", encoding="utf-8")
    first = scan_local_folder(first_root, "shared-label")[0]
    second = scan_local_folder(second_root, "shared-label")[0]
    other_label = scan_local_folder(first_root, "different-label")[0]
    assert first["relative_path"] == second["relative_path"] == "nested/record.csv"
    assert first["source_id"] == second["source_id"]
    assert first["source_id"] != other_label["source_id"]
    assert str(tmp_path) not in first["source_id"]


def test_skips_symbolic_links_when_supported(tmp_path):
    target = tmp_path / "inside.txt"
    target.write_text("inside", encoding="utf-8")
    external = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    external.write_text("outside", encoding="utf-8")
    link = tmp_path / "linked.txt"
    linked_dir = tmp_path / "linked-dir"
    try:
        link.symlink_to(external)
        linked_dir.symlink_to(external.parent, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable on this platform")
    try:
        files = scan_local_folder(tmp_path, "root-a")
        assert [item["relative_path"] for item in files] == ["inside.txt"]
    finally:
        external.unlink(missing_ok=True)


def test_rejects_symbolic_link_as_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "root-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are unavailable on this platform")
    with pytest.raises(ValueError, match="symbolic link"):
        scan_local_folder(link, "root-a")


def test_directory_permission_error_is_not_reported_as_complete_scan(tmp_path, monkeypatch):
    def deny_walk(root, **kwargs):
        callback = kwargs["onerror"]
        callback(PermissionError("access denied", str(root)))
        return iter(())

    monkeypatch.setattr(local_drive.os, "walk", deny_walk)
    with pytest.raises(LocalDriveScanError, match="Unable to scan directory"):
        scan_local_folder(tmp_path, "root-a")


@pytest.mark.parametrize(("filename", "expected_terms"), [
    ("document_001.pdf", ("QUOTATION", "Northstar", "INR 14,600")),
    ("document_002.docx", ("Operations Planning Meeting Notes", "Attendees", "responsibilities")),
    ("document_003.xlsx", ("Campaign", "Search advertising", "impressions")),
    ("document_004.txt", ("KYC VERIFICATION CHECKLIST", "Pending", "proof-of-address")),
    ("document_005.csv", ("Invoice number", "Payment status", "SYN-INV-26041")),
])
def test_extracts_meaningful_text_from_each_sample_document(filename, expected_terms):
    scanned = next(item for item in scan_local_folder(SAMPLE_ROOT, "drive-sample")
                   if item["filename"] == filename)
    result = extract_scanned_file(SAMPLE_ROOT, scanned)
    assert result["extraction_status"] == "extracted"
    assert result["error_message"] is None
    assert all(term.casefold() in result["extracted_text"].casefold() for term in expected_terms)
    assert str(SAMPLE_ROOT) not in str(result)


def test_empty_txt_has_empty_status(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    scanned = scan_local_folder(tmp_path, "root-a")[0]
    result = extract_scanned_file(tmp_path, scanned)
    assert result["extraction_status"] == "empty"
    assert result["extracted_text"] == ""
    assert result["error_message"] == "The document is empty."


def test_corrupted_pdf_has_safe_extraction_error(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a valid PDF")
    scanned = scan_local_folder(tmp_path, "root-a")[0]
    result = extract_scanned_file(tmp_path, scanned)
    assert result["extraction_status"] == "extraction_error"
    assert result["extracted_text"] == ""
    assert result["error_message"]
    assert str(tmp_path) not in result["error_message"]


def test_pdf_without_extractable_text_is_reported(tmp_path):
    from io import BytesIO
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = BytesIO()
    writer.write(buffer)
    (tmp_path / "blank.pdf").write_bytes(buffer.getvalue())
    scanned = scan_local_folder(tmp_path, "root-a")[0]
    result = extract_scanned_file(tmp_path, scanned)
    assert result["extraction_status"] == "no_text"
    assert result["extracted_text"] == ""


def test_unsupported_extension_is_not_read_or_extracted(tmp_path):
    path = tmp_path / "archive.zip"
    path.write_bytes(b"arbitrary data")
    scanned = scan_local_folder(tmp_path, "root-a")[0]
    result = extract_scanned_file(tmp_path, scanned)
    assert result["extraction_status"] == "unsupported"
    assert result["extracted_text"] == ""


def test_missing_file_after_scan_is_reported(tmp_path):
    path = tmp_path / "vanished.txt"
    path.write_text("sample text", encoding="utf-8")
    scanned = scan_local_folder(tmp_path, "root-a")[0]
    path.unlink()
    result = extract_scanned_file(tmp_path, scanned)
    assert result["extraction_status"] == "missing"
    assert result["error_message"] == "The file disappeared after scanning."


def test_unexpected_extractor_failure_is_reported_safely(tmp_path, monkeypatch):
    path = tmp_path / "notes.txt"
    path.write_text("sample text", encoding="utf-8")
    scanned = scan_local_folder(tmp_path, "root-a")[0]

    def fail_extraction(source, filename):
        raise RuntimeError(f"private path: {tmp_path}")

    monkeypatch.setattr(local_drive, "extract_text_with_locations", fail_extraction)
    result = extract_scanned_file(tmp_path, scanned)
    assert result["extraction_status"] == "extraction_error"
    assert result["error_message"] == "The document could not be extracted; it may be corrupted or invalid."
    assert str(tmp_path) not in result["error_message"]


def test_extraction_refuses_symbolic_link_added_after_scan(tmp_path):
    target = tmp_path.parent / f"{tmp_path.name}-target.txt"
    target.write_text("outside root", encoding="utf-8")
    link = tmp_path / "linked.txt"
    try:
        scanned = {"source_id": "id", "relative_path": "linked.txt", "filename": "linked.txt",
                   "extension": ".txt", "file_status": "ready"}
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        target.unlink(missing_ok=True)
        pytest.skip("Symbolic links are unavailable on this platform")
    try:
        result = extract_scanned_file(tmp_path, scanned)
        assert result["extraction_status"] == "invalid_path"
        assert str(target) not in str(result)
    finally:
        target.unlink(missing_ok=True)


EXPECTED_CATEGORIES = {
    "document_001.pdf": "sales",
    "document_002.docx": "office",
    "document_003.xlsx": "marketing",
    "document_004.txt": "verification",
    "document_005.csv": "accounts",
}


def test_imports_all_sample_documents_with_expected_categories_and_metadata():
    import json
    from src.connector_intelligence.db import connect

    conn = connect(":memory:")
    try:
        report = import_local_folder(SAMPLE_ROOT, "drive-sample", conn)
        assert report["files_scanned"] == 5
        assert report["counts"]["imported"] == 5
        assert all(report["counts"][status] == 0 for status in report["counts"] if status != "imported")
        assert {item["filename"]: item["category"] for item in report["results"]} == EXPECTED_CATEGORIES

        rows = conn.execute(
            "SELECT source,source_id,title,content,category,metadata_json FROM records ORDER BY title"
        ).fetchall()
        assert len(rows) == 5
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            assert row["source"] == "drive"
            assert row["category"] == EXPECTED_CATEGORIES[row["title"]]
            assert metadata["origin"] == "local_folder"
            assert metadata["relative_path"]
            assert str(SAMPLE_ROOT) not in row["metadata_json"]
            assert row["content"].strip()
    finally:
        conn.close()


def test_repeated_import_is_idempotent_and_reports_existing_ids():
    from src.connector_intelligence.db import connect

    conn = connect(":memory:")
    try:
        first = import_local_folder(SAMPLE_ROOT, "drive-sample", conn)
        ids_before = [row[0] for row in conn.execute(
            "SELECT source_id FROM records WHERE source='drive' ORDER BY source_id"
        )]
        second = import_local_folder(SAMPLE_ROOT, "drive-sample", conn)
        ids_after = [row[0] for row in conn.execute(
            "SELECT source_id FROM records WHERE source='drive' ORDER BY source_id"
        )]
        assert first["counts"]["imported"] == 5
        assert second["counts"]["already_imported"] == 5
        assert ids_after == ids_before
        assert conn.execute("SELECT COUNT(*) FROM records WHERE source='drive'").fetchone()[0] == 5
    finally:
        conn.close()


def test_one_extraction_failure_does_not_stop_other_files(tmp_path, monkeypatch):
    from src.connector_intelligence.db import connect

    (tmp_path / "a.txt").write_text("campaign budget and marketing channels", encoding="utf-8")
    (tmp_path / "b.txt").write_text("invoice payment accounts", encoding="utf-8")
    real_extract = local_drive.extract_scanned_file

    def fail_one(root, info):
        if info["filename"] == "a.txt":
            raise RuntimeError("sensitive local path")
        return real_extract(root, info)

    monkeypatch.setattr(local_drive, "extract_scanned_file", fail_one)
    conn = connect(":memory:")
    try:
        report = import_local_folder(tmp_path, "fault-test", conn)
        assert report["counts"]["extraction_error"] == 1
        assert report["counts"]["imported"] == 1
        assert {item["filename"] for item in report["results"]} == {"a.txt", "b.txt"}
        assert conn.execute("SELECT COUNT(*) FROM records WHERE source='drive'").fetchone()[0] == 2
        failed = next(item for item in report["results"] if item["filename"] == "a.txt")
        assert str(tmp_path) not in str(failed)
    finally:
        conn.close()


def test_unsupported_and_empty_files_have_individual_statuses_and_counts(tmp_path):
    from src.connector_intelligence.db import connect

    (tmp_path / "archive.zip").write_bytes(b"not extracted")
    (tmp_path / "blank.txt").write_bytes(b"")
    conn = connect(":memory:")
    try:
        report = import_local_folder(tmp_path, "status-test", conn)
        assert report["counts"]["unsupported"] == 1
        assert report["counts"]["empty"] == 1
        assert report["counts"]["imported"] == 0
        assert {item["status"] for item in report["results"]} == {"unsupported", "empty"}
        assert conn.execute("SELECT COUNT(*) FROM records WHERE source='drive'").fetchone()[0] == 2
    finally:
        conn.close()


def test_content_not_filename_or_folder_name_drives_classification(tmp_path):
    from src.connector_intelligence.db import connect

    nested = tmp_path / "sales_folder"
    nested.mkdir()
    (nested / "invoice_customer.txt").write_text(
        "Marketing campaign channels planned expenditure newsletter budget",
        encoding="utf-8",
    )
    conn = connect(":memory:")
    try:
        report = import_local_folder(tmp_path, "root-label", conn)
        assert report["results"][0]["category"] == "marketing"
    finally:
        conn.close()


def test_imported_content_is_searchable_through_query_layer():
    from src.connector_intelligence.db import connect
    from src.connector_intelligence.query import retrieve

    conn = connect(":memory:")
    try:
        import_local_folder(SAMPLE_ROOT, "drive-sample", conn)
        results = retrieve(conn, "Northstar quotation rental vehicle", source="drive")
        assert any(item["title"] == "document_001.pdf" for item in results)
    finally:
        conn.close()



def test_classification_error_is_recorded_and_other_files_continue(tmp_path, monkeypatch):
    from src.connector_intelligence.db import connect

    (tmp_path / "a.txt").write_text("first synthetic content", encoding="utf-8")
    (tmp_path / "b.txt").write_text("second synthetic content", encoding="utf-8")

    def fail_classification(name, text):
        raise RuntimeError("classifier unavailable")

    monkeypatch.setattr(local_drive, "classify_document", fail_classification)
    conn = connect(":memory:")
    try:
        report = import_local_folder(tmp_path, "classification-failure", conn)
        assert report["counts"]["classification_error"] == 2
        assert all(item["status"] == "classification_error" for item in report["results"])
        assert conn.execute("SELECT COUNT(*) FROM records WHERE source='drive'").fetchone()[0] == 2
    finally:
        conn.close()


def test_storage_error_is_recorded_without_stopping_other_files(tmp_path, monkeypatch):
    from src.connector_intelligence.db import connect

    (tmp_path / "a.txt").write_text("first synthetic content", encoding="utf-8")
    (tmp_path / "b.txt").write_text("second synthetic content", encoding="utf-8")
    real_store = local_drive._store_result

    def fail_one_store(conn, source_id, title, text, category, metadata, file_metadata):
        if title == "a.txt":
            raise RuntimeError("database write failed")
        return real_store(conn, source_id, title, text, category, metadata, file_metadata)

    monkeypatch.setattr(local_drive, "_store_result", fail_one_store)
    conn = connect(":memory:")
    try:
        report = import_local_folder(tmp_path, "storage-failure", conn)
        assert report["counts"]["storage_error"] == 1
        assert report["counts"]["imported"] == 1
        assert conn.execute("SELECT COUNT(*) FROM records WHERE source='drive'").fetchone()[0] == 1
        failed = next(item for item in report["results"] if item["filename"] == "a.txt")
        assert failed["error_message"] == "The document record could not be stored."
    finally:
        conn.close()
