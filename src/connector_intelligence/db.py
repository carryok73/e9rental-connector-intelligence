import sqlite3
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL CHECK(source IN ('whatsapp','gmail','drive')),
  source_id TEXT NOT NULL,
  parent_id TEXT,
  occurred_at TEXT,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  category TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_records_source_time ON records(source, occurred_at);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(title, content, content='records', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
  INSERT INTO records_fts(rowid,title,content) VALUES(new.id,new.title,new.content);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
  INSERT INTO records_fts(records_fts,rowid,title,content) VALUES('delete',old.id,old.title,old.content);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE ON records BEGIN
  INSERT INTO records_fts(records_fts,rowid,title,content) VALUES('delete',old.id,old.title,old.content);
  INSERT INTO records_fts(rowid,title,content) VALUES(new.id,new.title,new.content);
END;
"""


def connect(path: str = "data/connector_intelligence.db") -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def whatsapp_group_filter(selected_group_ids: set[str] | None) -> tuple[str, tuple[str, ...]]:
    """Return a parameterized SQL predicate for currently visible WhatsApp rows."""
    groups = sorted(selected_group_ids or set())
    if not groups:
        return "0", ()
    placeholders = ",".join("?" for _ in groups)
    return f"parent_id IN ({placeholders})", tuple(groups)


def source_records_filter(source: str, selected_group_ids: set[str] | None = None) -> tuple[str, tuple[str, ...]]:
    """Build the source predicate, applying the current allowlist to WhatsApp."""
    if source == "whatsapp":
        group_filter, group_params = whatsapp_group_filter(selected_group_ids)
        return f"source=? AND {group_filter}", (source, *group_params)
    return "source=?", (source,)


def count_source_records(conn: sqlite3.Connection, source: str,
                         selected_group_ids: set[str] | None = None) -> int:
    where, params = source_records_filter(source, selected_group_ids)
    row = conn.execute(f"SELECT COUNT(*) FROM records WHERE {where}", params).fetchone()
    return int(row[0])


def read_source_records(conn: sqlite3.Connection, source: str,
                        selected_group_ids: set[str] | None = None,
                        limit: int = 100) -> list[sqlite3.Row]:
    where, params = source_records_filter(source, selected_group_ids)
    return conn.execute(
        "SELECT source_id,occurred_at,title,content,category,metadata_json "
        f"FROM records WHERE {where} ORDER BY occurred_at DESC LIMIT ?",
        (*params, max(0, int(limit))),
    ).fetchall()


def upsert_record(conn: sqlite3.Connection, *, source: str, source_id: str, title: str = "",
                  content: str = "", occurred_at: str | None = None, parent_id: str | None = None,
                  category: str | None = None, metadata_json: str = "{}") -> int:
    conn.execute("""INSERT INTO records(source,source_id,parent_id,occurred_at,title,content,category,metadata_json)
      VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source,source_id) DO UPDATE SET parent_id=excluded.parent_id,
      occurred_at=excluded.occurred_at,title=excluded.title,content=excluded.content,
      category=excluded.category,metadata_json=excluded.metadata_json""",
      (source, source_id, parent_id, occurred_at, title, content, category, metadata_json))
    row = conn.execute("SELECT id FROM records WHERE source=? AND source_id=?", (source, source_id)).fetchone()
    return int(row[0])

