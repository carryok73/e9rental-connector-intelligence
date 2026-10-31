from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest

from src.connector_intelligence import config
from src.connector_intelligence.db import connect


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app.py"
SAMPLE_ROOT = PROJECT_ROOT / "data" / "imports" / "drive_sample"


def _patch_settings(monkeypatch, database_path):
    def make_settings():
        return SimpleNamespace(
            database_path=str(database_path),
            output_dir=str(database_path.parent / "outputs"),
            google_client_secrets_file="missing-local-test-credentials.json",
            gmail_mailbox_owner="",
            google_drive_account="",
            selected_whatsapp_groups=lambda: set(),
            reference_day=lambda: date(2026, 10, 31),
        )

    monkeypatch.setattr(config, "Settings", make_settings)


def _button(app_test, label):
    return next(button for button in app_test.button if button.label == label)


def test_dashboard_loads_and_imports_sample_documents_twice(tmp_path, monkeypatch):
    database_path = tmp_path / "dashboard.sqlite"
    _patch_settings(monkeypatch, database_path)

    app_test = AppTest.from_file(str(APP_PATH)).run()
    assert not app_test.exception
    assert any(item.key == "local_drive_folder_path" for item in app_test.text_input)
    assert any(item.label == "Import Documents" for item in app_test.button)

    app_test.text_input(key="local_drive_folder_path").set_value(str(SAMPLE_ROOT))
    app_test.text_input(key="local_drive_root_label").set_value("dashboard-sample")
    app_test = _button(app_test, "Import Documents").click().run()
    assert not app_test.exception

    conn = connect(str(database_path))
    try:
        rows = conn.execute(
            "SELECT source,source_id,title,category,metadata_json FROM records ORDER BY title"
        ).fetchall()
        assert len(rows) == 5
        assert all(row["source"] == "drive" for row in rows)
        assert {row["category"] for row in rows} == {
            "sales", "office", "marketing", "verification", "accounts",
        }
        assert all('"origin": "local_folder"' in row["metadata_json"] for row in rows)
        first_ids = {row["source_id"] for row in rows}
    finally:
        conn.close()

    export_path = database_path.parent / "outputs" / "drive_output.json"
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert len(exported) == 5
    assert all(set(item) == {
        "source_id", "parent_id", "occurred_at", "title", "content", "category", "metadata",
    } for item in exported)
    assert {item["category"] for item in exported} == {
        "sales", "office", "marketing", "verification", "accounts",
    }
    assert {item["source_id"] for item in exported} == first_ids
    assert all(item["metadata"]["origin"] == "local_folder" for item in exported)
    assert all(item["metadata"]["relative_path"] for item in exported)
    assert str(SAMPLE_ROOT) not in json.dumps(exported)
    assert str(tmp_path) not in json.dumps(exported)
    assert all(item["content"].strip() for item in exported)

    app_test = _button(app_test, "Import Documents").click().run()
    assert not app_test.exception
    conn = connect(str(database_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM records WHERE source='drive'").fetchone()[0] == 5
        second_ids = {row[0] for row in conn.execute(
            "SELECT source_id FROM records WHERE source='drive'")}
    finally:
        conn.close()
    exported_again = json.loads(export_path.read_text(encoding="utf-8"))
    assert len(exported_again) == 5
    assert {item["source_id"] for item in exported_again} == first_ids == second_ids
    assert any(metric.label == "Already imported" and str(metric.value) == "5"
               for metric in app_test.metric)
    assert any(metric.label == "Total discovered" and str(metric.value) == "5"
               for metric in app_test.metric)


def test_dashboard_reports_invalid_folder_without_echoing_path(tmp_path, monkeypatch):
    database_path = tmp_path / "invalid-folder.sqlite"
    _patch_settings(monkeypatch, database_path)
    app_test = AppTest.from_file(str(APP_PATH)).run()
    private_path = str(tmp_path / "missing-private-folder")
    app_test.text_input(key="local_drive_folder_path").set_value(private_path)
    app_test = _button(app_test, "Import Documents").click().run()
    assert not app_test.exception
    errors = [element.value for element in app_test.error]
    assert any("not found" in error for error in errors)
    assert all(private_path not in error for error in errors)
