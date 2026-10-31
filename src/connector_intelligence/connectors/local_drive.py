"""Metadata-only scanner for files in a user-selected local Drive folder."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .drive import (DRIVE_CATEGORIES, UnsupportedDriveFile, classify_document,
                    extract_text, extract_text_with_locations)
from .drive_store import _store_result

SUPPORTED_EXTENSIONS = frozenset({".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md", ".markdown"})


class LocalDriveScanError(RuntimeError):
    """Raised when a requested root or one of its directories cannot be scanned."""


def _source_id(root_label: str, relative_path: str) -> str:
    """Create a stable, path-independent identifier from the caller label and relative path."""
    identity = f"{root_label.strip()}\0{relative_path}".encode("utf-8")
    return "local-" + hashlib.sha256(identity).hexdigest()


def _scan_error(root: Path, error: OSError) -> None:
    """Turn os.walk errors into a safe message that does not expose an absolute path."""
    path = Path(error.filename) if error.filename else root
    try:
        relative = path.relative_to(root).as_posix() or "."
    except ValueError:
        relative = "."
    raise LocalDriveScanError(f"Unable to scan directory {relative!r}.") from error


def scan_local_folder(root_directory: str | Path, root_label: str) -> list[dict[str, Any]]:
    """Recursively list supported and unsupported regular files without reading contents.

    Symbolic links are excluded, including linked directories. An inaccessible directory
    raises LocalDriveScanError so callers cannot mistake a partial scan for a full scan.
    Individual files that disappear or fail metadata access remain in the result with a
    corresponding ``file_status``.
    """
    label = str(root_label).strip()
    if not label:
        raise ValueError("root_label must be a non-empty caller-provided label")

    requested_root = Path(root_directory).expanduser()
    if requested_root.is_symlink():
        raise ValueError("root_directory must not be a symbolic link")
    try:
        root = requested_root.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError("The requested root directory does not exist.") from None
    except OSError as exc:
        raise LocalDriveScanError("The requested root directory cannot be accessed.") from exc
    if not root.is_dir():
        raise NotADirectoryError("The requested root path is not a directory.")

    discovered: list[dict[str, Any]] = []
    for current, directory_names, filenames in os.walk(root, topdown=True, followlinks=False,
                                                        onerror=lambda error: _scan_error(root, error)):
        current_path = Path(current)
        # os.walk does not descend into linked directories when followlinks=False, but
        # pruning explicitly also ensures they are not considered as scan targets.
        directory_names[:] = sorted(
            name for name in directory_names
            if not (current_path / name).is_symlink()
        )
        for filename in sorted(filenames):
            path = current_path / filename
            if path.is_symlink():
                continue
            try:
                relative_path = path.relative_to(root).as_posix()
            except ValueError:
                # Defensive containment guard in case a platform path implementation
                # yields an unexpected traversal result.
                continue
            extension = path.suffix.lower()
            status = "ready" if extension in SUPPORTED_EXTENSIONS else "unsupported"
            try:
                file_stat = path.lstat()
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                size: int | None = file_stat.st_size
            except FileNotFoundError:
                status, size = "missing", None
            except PermissionError:
                status, size = "permission_error", None
            discovered.append({
                "source_id": _source_id(label, relative_path),
                "relative_path": relative_path,
                "filename": filename,
                "extension": extension,
                "file_size": size,
                "file_status": status,
            })

    return sorted(discovered, key=lambda item: item["relative_path"].casefold())



def extract_scanned_file(root_directory: str | Path,
                         file_info: Mapping[str, Any]) -> dict[str, Any]:
    """Read and extract one scanner result using the existing Drive text extractor.

    Only file metadata is returned alongside text and a safe status message. The
    absolute root path is used to access the file but is never included in results.
    """
    raw_relative = str(file_info.get("relative_path", ""))
    normalized_relative = raw_relative.replace("\\", "/")
    relative = PurePosixPath(normalized_relative)
    safe_relative = (
        bool(normalized_relative)
        and not relative.is_absolute()
        and all(part not in {"", ".", ".."} for part in relative.parts)
        and "\\" not in raw_relative
    )
    relative_path = relative.as_posix() if safe_relative else ""
    raw_filename = str(file_info.get("filename", ""))
    filename = PurePosixPath(raw_filename.replace("\\", "/")).name
    extension = str(file_info.get("extension", Path(filename).suffix)).lower()
    base = {
        "source_id": str(file_info.get("source_id", "")),
        "relative_path": relative_path,
        "filename": filename,
        "extension": extension,
    }

    def result(status: str, text: str = "", error: str | None = None) -> dict[str, Any]:
        return {**base, "extraction_status": status, "extracted_text": text,
                "error_message": error}

    if not safe_relative:
        return result("invalid_path", error="The scanned relative path is invalid.")
    if extension not in SUPPORTED_EXTENSIONS:
        return result("unsupported", error="This file extension is not supported for text extraction.")
    if str(file_info.get("file_status", "ready")) == "unsupported":
        return result("unsupported", error="This file extension is not supported for text extraction.")

    requested_root = Path(root_directory).expanduser()
    try:
        if requested_root.is_symlink():
            return result("invalid_path", error="The root directory must not be a symbolic link.")
        root = requested_root.resolve(strict=True)
        if not root.is_dir():
            return result("invalid_path", error="The requested root path is not a directory.")
    except FileNotFoundError:
        return result("missing", error="The root directory no longer exists.")
    except OSError:
        return result("read_error", error="The root directory cannot be accessed.")

    candidate = root.joinpath(*relative.parts)
    # Check every path component before opening. Linked files and directories are
    # rejected so extraction cannot traverse a link outside the selected root.
    component = root
    for part in relative.parts:
        component = component / part
        try:
            if component.is_symlink():
                return result("invalid_path", error="Symbolic links are not read during extraction.")
        except OSError:
            return result("read_error", error="The file path cannot be inspected.")

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return result("missing", error="The file disappeared after scanning.")
    except PermissionError:
        return result("read_error", error="The file cannot be accessed due to permissions.")
    except OSError:
        return result("read_error", error="The file path cannot be accessed.")
    try:
        if os.path.commonpath((str(root), str(resolved))) != str(root):
            return result("invalid_path", error="The scanned file is outside the selected root.")
    except ValueError:
        return result("invalid_path", error="The scanned file is outside the selected root.")

    try:
        data = candidate.read_bytes()
    except FileNotFoundError:
        return result("missing", error="The file disappeared after scanning.")
    except PermissionError:
        return result("read_error", error="The file cannot be read due to permissions.")
    except OSError:
        return result("read_error", error="The file could not be read.")

    if not data:
        return result("empty", error="The document is empty.")

    # The existing extractor recognizes .md; map the equivalent .markdown suffix
    # for extraction without changing the caller-visible filename or extension.
    extractor_name = filename[:-len(extension)] + ".md" if extension == ".markdown" else filename
    try:
        extraction = extract_text_with_locations(data, extractor_name)
        text = extraction["text"]
    except UnsupportedDriveFile:
        return result("unsupported", error="This file extension is not supported for text extraction.")
    except Exception:
        return result("extraction_error", error="The document could not be extracted; it may be corrupted or invalid.")

    if not text.strip():
        if extension == ".pdf":
            return result("no_text", error="No extractable text was found; the PDF may be image-only or blank.")
        return result("empty", error="The document contains no extractable text.")
    extracted = result("extracted", text=text)
    extracted["source_locations"] = extraction.get("locations", [])
    return extracted



IMPORT_STATUSES = (
    "imported", "already_imported", "unsupported", "empty", "no_text",
    "extraction_error", "classification_error", "storage_error",
)


def _previous_local_import(conn: sqlite3.Connection, source_id: str) -> tuple[bool, bool]:
    """Return (record exists, it belongs to this local-folder importer)."""
    row = conn.execute(
        "SELECT metadata_json FROM records WHERE source=? AND source_id=?",
        ("drive", source_id),
    ).fetchone()
    if row is None:
        return False, False
    try:
        metadata = json.loads(row[0])
    except (TypeError, ValueError):
        return True, False
    return True, isinstance(metadata, dict) and metadata.get("origin") == "local_folder"


def import_local_folder(root_directory: str | Path, root_label: str,
                        conn: sqlite3.Connection) -> dict[str, Any]:
    """Extract, classify, and upsert one scanned folder into the existing Drive source.

    Every scanned file receives an individual result. Failures are isolated per file;
    the returned counts include zero-valued entries for each documented import status.
    Local metadata contains relative paths only.
    """
    scanned_files = scan_local_folder(root_directory, root_label)
    counts = {status: 0 for status in IMPORT_STATUSES}
    results: list[dict[str, Any]] = []

    for scanned in scanned_files:
        source_id = str(scanned.get("source_id", ""))
        relative_path = str(scanned.get("relative_path", ""))
        filename = str(scanned.get("filename", ""))
        extension = str(scanned.get("extension", "")).lower()
        category: str | None = None
        category_reason: str | None = None
        error_message: str | None = None

        try:
            extraction = extract_scanned_file(root_directory, scanned)
            extraction_status = str(extraction.get("extraction_status", "extraction_error"))
            content = str(extraction.get("extracted_text", ""))
            error_message = extraction.get("error_message")
        except Exception:
            extraction_status = "extraction_error"
            content = ""
            error_message = "The document could not be extracted."

        if extraction_status == "extracted":
            try:
                # An empty name ensures the classifier can only use extracted content.
                category, category_reason = classify_document("", content)
                if category not in DRIVE_CATEGORIES:
                    raise ValueError("Classifier returned an unknown category.")
                status = "imported"
            except Exception:
                category = None
                category_reason = None
                status = "classification_error"
                error_message = "The extracted document could not be classified."
        elif extraction_status in {"unsupported", "empty", "no_text"}:
            status = extraction_status
        else:
            status = "extraction_error"
            error_message = error_message or "The document could not be extracted."

        try:
            exists, is_local = _previous_local_import(conn, source_id)
            if exists and not is_local:
                raise RuntimeError("The source ID is already used by a non-local Drive record.")
            if status == "imported" and is_local:
                status = "already_imported"

            stored_metadata = {
                "origin": "local_folder",
                "relative_path": relative_path,
                "extension": extension,
                "file_size": scanned.get("file_size"),
                "status": "extracted" if status in {"imported", "already_imported"} else status,
                "import_status": "imported" if status in {"imported", "already_imported"} else status,
                "extraction_status": extraction_status,
                "category_reason": category_reason,
                "extraction_error": error_message,
            }
            if extraction_status == "extracted":
                stored_metadata["source_locations"] = extraction.get("source_locations", [])
            with conn:
                _store_result(conn, source_id, filename, content, category,
                              stored_metadata, {})
        except Exception:
            status = "storage_error"
            category = None
            error_message = "The document record could not be stored."

        counts[status] = counts.get(status, 0) + 1
        results.append({
            "source_id": source_id,
            "relative_path": relative_path,
            "filename": filename,
            "extension": extension,
            "status": status,
            "category": category,
            "error_message": error_message,
        })

    return {"files_scanned": len(scanned_files), "counts": counts, "results": results}
