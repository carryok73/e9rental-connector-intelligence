"""Drive file processing, SQLite storage, and output synchronization."""
import json
import sqlite3
from pathlib import Path
from typing import Any

from ..db import upsert_record
from ..exports import export_source
from .drive import (
    DriveConnectorError,
    InaccessibleDriveFile,
    UnsupportedDriveFile,
    authorized_drive_account,
    classify_document,
    download_drive_file,
    extract_text_with_locations,
    list_drive_files,
)


def _store_result(conn: sqlite3.Connection, file_id: str, title: str, text: str,
                  category: str | None, metadata: dict[str, Any], file_metadata: dict[str, Any]) -> dict[str, Any]:
    upsert_record(
        conn,
        source="drive",
        source_id=file_id,
        title=title,
        content=text,
        occurred_at=file_metadata.get("modifiedTime"),
        parent_id=(file_metadata.get("parents") or [None])[0],
        category=category,
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    return {"source_id": file_id, "title": title, "category": category, **metadata}


def _source_metadata(file_metadata: dict[str, Any]) -> dict[str, Any]:
    capabilities = file_metadata.get("capabilities") or {}
    return {
        "mime_type": file_metadata.get("mimeType"),
        "modified_time": file_metadata.get("modifiedTime"),
        "created_time": file_metadata.get("createdTime"),
        "web_view_link": file_metadata.get("webViewLink"),
        "description": file_metadata.get("description"),
        "size": file_metadata.get("size"),
        "md5_checksum": file_metadata.get("md5Checksum"),
        "parents": file_metadata.get("parents", []),
        "drive_id": file_metadata.get("driveId"),
        "can_download": capabilities.get("canDownload"),
    }


def ingest_drive_file(conn: sqlite3.Connection, service: Any, file_metadata: dict[str, Any]) -> dict[str, Any]:
    """Download/export, extract, classify, and upsert one Drive file.

    Errors are represented in metadata and do not claim extraction succeeded. File
    content stays in memory during processing; only extracted text is persisted.
    """
    file_id = str(file_metadata.get("id", ""))
    name = str(file_metadata.get("name", "(unnamed Drive file)"))
    metadata = _source_metadata(file_metadata)
    if not file_id:
        return {"source_id": "", "title": name, "category": None,
                "status": "invalid_metadata", "category_reason": None,
                "extraction_error": "Drive file metadata did not include a file ID."}
    try:
        content, extraction_name = download_drive_file(service, file_metadata)
    except InaccessibleDriveFile as exc:
        metadata.update(status="inaccessible", category_reason=None, extraction_error=str(exc))
        return _store_result(conn, file_id, name, "", None, metadata, file_metadata)
    except UnsupportedDriveFile as exc:
        metadata.update(status="unsupported", category_reason=None, extraction_error=str(exc))
        return _store_result(conn, file_id, name, "", None, metadata, file_metadata)
    except DriveConnectorError as exc:
        metadata.update(status="api_error", category_reason=None, extraction_error=str(exc))
        return _store_result(conn, file_id, name, "", None, metadata, file_metadata)

    if not content:
        metadata.update(status="empty", category_reason=None, extraction_error="Drive returned an empty file.")
        return _store_result(conn, file_id, name, "", None, metadata, file_metadata)
    try:
        extraction = extract_text_with_locations(content, extraction_name)
        text = extraction["text"]
    except DriveConnectorError as exc:
        metadata.update(status="unsupported", category_reason=None, extraction_error=str(exc))
        return _store_result(conn, file_id, name, "", None, metadata, file_metadata)
    except Exception:
        metadata.update(status="extraction_error", category_reason=None,
                        extraction_error="The downloaded content could not be extracted as text.")
        return _store_result(conn, file_id, name, "", None, metadata, file_metadata)

    category, reason = classify_document(name, text)
    metadata.update(
        status="extracted" if text.strip() else "no_text",
        category_reason=reason,
        extraction_error=None if text.strip() else "No text was found in this file; it may be blank or image-only.",
        extracted_characters=len(text),
        source_locations=extraction.get("locations", []),
    )
    return _store_result(conn, file_id, name, text, category, metadata, file_metadata)


def sync_drive(conn: sqlite3.Connection, service: Any, *, expected_account: str,
               output_dir: str | Path = "outputs", page_size: int = 100) -> dict[str, int]:
    """Sync the configured authorized account, upsert source records, and export JSON."""
    expected = expected_account.strip().casefold()
    if not expected:
        raise DriveConnectorError("Set GOOGLE_DRIVE_ACCOUNT to the authorized sandbox account before syncing.")
    actual = authorized_drive_account(service)
    if actual != expected:
        raise DriveConnectorError("The authorized Drive account does not match GOOGLE_DRIVE_ACCOUNT; no files were listed.")

    files = list_drive_files(service, page_size=page_size)
    statuses: dict[str, int] = {}
    with conn:
        for metadata in files:
            result = ingest_drive_file(conn, service, metadata)
            status = str(result.get("status", "invalid_metadata"))
            statuses[status] = statuses.get(status, 0) + 1
    export_source(conn, "drive", Path(output_dir) / "drive_output.json")
    return {"files_listed": len(files), **{f"{key}_files": value for key, value in statuses.items()}}


def ingest_local_file(conn: sqlite3.Connection, source_id: str, path: str | Path) -> dict[str, Any]:
    """Backward-compatible local file helper retained for fixtures and development."""
    file = Path(path)
    try:
        text = extract_text(file)
        category, reason = classify_document(file.name, text)
        metadata = {"status": "extracted", "category_reason": reason, "extraction_error": None}
        result = _store_result(conn, source_id, file.name, text, category, metadata, {})
    except Exception:
        metadata = {"status": "extraction_error", "category_reason": None,
                    "extraction_error": "The local file could not be extracted as text."}
        result = _store_result(conn, source_id, file.name, "", None, metadata, {})
    conn.commit()
    return result
