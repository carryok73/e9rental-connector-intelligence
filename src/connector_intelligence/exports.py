import json
from pathlib import Path
import sqlite3

from .db import source_records_filter


def export_source(conn: sqlite3.Connection, source: str, path: str | Path,
                  selected_whatsapp_groups: set[str] | None = None) -> None:
    where, params = source_records_filter(source, selected_whatsapp_groups)
    rows = conn.execute(
        "SELECT source_id,parent_id,occurred_at,title,content,category,metadata_json "
        f"FROM records WHERE {where} ORDER BY occurred_at", params,
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json"))
        except (ValueError, TypeError):
            item["metadata"] = {}
            item.pop("metadata_json", None)
        items.append(item)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")


def export_all(conn: sqlite3.Connection, output_dir: str | Path,
               selected_whatsapp_groups: set[str] | None = None) -> None:
    for source in ("whatsapp", "gmail", "drive"):
        export_source(conn, source, Path(output_dir) / f"{source}_output.json",
                      selected_whatsapp_groups=selected_whatsapp_groups)

