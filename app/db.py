"""SQLite storage with FTS5 full-text search over document chunks."""

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    filename TEXT,
    source_type TEXT NOT NULL,
    uploaded_by TEXT,
    created_at TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    content,
    title,
    doc_id UNINDEXED,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user TEXT,
    rating TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    sources TEXT
);

CREATE TABLE IF NOT EXISTS vectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    vec TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_doc ON vectors(doc_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    user TEXT,
    question TEXT NOT NULL,
    answer TEXT,
    sources TEXT,
    answered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queries_created ON queries(created_at);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        # Migration: store the full original text so docs can be viewed/edited.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
        if "content" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN content TEXT")


def _reconstruct(chunk_texts: list[str]) -> str:
    """Best-effort rejoin of overlapping chunks for documents saved before the
    content column existed. Exact for single-chunk docs; de-overlaps multi-chunk."""
    out = ""
    for c in chunk_texts:
        if not out:
            out = c
            continue
        max_ov = min(len(out), len(c), 400)
        overlap = 0
        for k in range(max_ov, 20, -1):
            if out[-k:] == c[:k]:
                overlap = k
                break
        out += c[overlap:] if overlap else ("\n\n" + c)
    return out


def add_document(title: str, filename: str | None, source_type: str,
                 uploaded_by: str | None, chunks: list[str],
                 content: str | None = None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO documents (title, filename, source_type, uploaded_by, created_at, chunk_count, content)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, filename, source_type, uploaded_by,
             datetime.now(timezone.utc).isoformat(), len(chunks), content),
        )
        doc_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO chunks (content, title, doc_id) VALUES (?, ?, ?)",
            [(c, title, doc_id) for c in chunks],
        )
    _index_document(doc_id, chunks)
    return doc_id


def _index_document(doc_id: int, chunks: list[str]) -> None:
    """Compute and store semantic embeddings for a document's chunks.
    Best-effort: if the embedding service is unavailable, keyword search still works."""
    try:
        from . import semantic
        semantic.index_document(doc_id, chunks)
    except Exception:
        pass


def get_document(doc_id: int) -> dict | None:
    """Return a single document with its full editable content."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, title, source_type, content, chunk_count FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            return None
        doc = dict(row)
        if doc["content"] is None:  # legacy doc — rebuild from chunks
            chunk_texts = [
                r["content"] for r in conn.execute(
                    "SELECT content FROM chunks WHERE doc_id = ? ORDER BY rowid", (doc_id,)
                ).fetchall()
            ]
            doc["content"] = _reconstruct(chunk_texts)
        return doc


def update_document(doc_id: int, title: str, content: str, chunks: list[str]) -> bool:
    """Replace a document's title and content, re-indexing its search chunks."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE documents SET title = ?, content = ?, chunk_count = ? WHERE id = ?",
            (title, content, len(chunks), doc_id),
        )
        if cur.rowcount == 0:
            return False
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO chunks (content, title, doc_id) VALUES (?, ?, ?)",
            [(c, title, doc_id) for c in chunks],
        )
    _index_document(doc_id, chunks)
    return True


def list_documents() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, filename, source_type, uploaded_by, created_at, chunk_count"
            " FROM documents ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_document(doc_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM vectors WHERE doc_id = ?", (doc_id,))
        return cur.rowcount > 0


def add_feedback(user: str | None, rating: str, question: str,
                 answer: str, sources: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO feedback (created_at, user, rating, question, answer, sources)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), user, rating, question, answer, sources),
        )
        return cur.lastrowid


def list_feedback(limit: int = 200) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, user, rating, question, answer, sources"
            " FROM feedback ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def feedback_counts() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT rating, COUNT(*) AS c FROM feedback GROUP BY rating").fetchall()
        return {r["rating"]: r["c"] for r in rows}


def delete_feedback(fid: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM feedback WHERE id = ?", (fid,))
        return cur.rowcount > 0


def get_chunks(doc_id: int) -> list[str]:
    with get_conn() as conn:
        return [
            r["content"] for r in conn.execute(
                "SELECT content FROM chunks WHERE doc_id = ? ORDER BY rowid", (doc_id,)
            ).fetchall()
        ]


def replace_vectors(doc_id: int, items: list[tuple[str, str]]) -> None:
    """items: list of (content, vec_json). Replaces all vectors for the document."""
    with get_conn() as conn:
        conn.execute("DELETE FROM vectors WHERE doc_id = ?", (doc_id,))
        conn.executemany(
            "INSERT INTO vectors (doc_id, content, vec) VALUES (?, ?, ?)",
            [(doc_id, c, v) for c, v in items],
        )


def delete_vectors(doc_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM vectors WHERE doc_id = ?", (doc_id,))


def all_vectors() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT v.doc_id, d.title, v.content, v.vec FROM vectors v"
            " JOIN documents d ON d.id = v.doc_id"
        ).fetchall()
        return [dict(r) for r in rows]


def get_setting(key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value, updated_at, updated_by FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None


def set_setting(key: str, value: str, updated_by: str | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (key, value, datetime.now(timezone.utc).isoformat(), updated_by),
        )


def log_query(user: str | None, question: str, answer: str,
              sources: str, answered: bool) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO queries (created_at, user, question, answer, sources, answered)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), user, question, answer,
             sources, 1 if answered else 0),
        )
        return cur.lastrowid


def list_queries(limit: int = 500) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, user, question, answer, sources, answered"
            " FROM queries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def all_queries() -> list[dict]:
    """Every logged question, oldest first — used for CSV export."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, user, question, answer, sources, answered"
            " FROM queries ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def query_stats() -> dict:
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM queries").fetchone()["c"]
        users = conn.execute("SELECT COUNT(DISTINCT user) AS c FROM queries").fetchone()["c"]
        unanswered = conn.execute("SELECT COUNT(*) AS c FROM queries WHERE answered = 0").fetchone()["c"]
        # questions per day, last 14 days (UTC date prefix of created_at)
        daily = conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c"
            " FROM queries GROUP BY day ORDER BY day DESC LIMIT 14"
        ).fetchall()
        return {
            "total": total,
            "users": users,
            "unanswered": unanswered,
            "daily": [dict(r) for r in daily],
        }


def search_chunks(query: str, limit: int = 8) -> list[dict]:
    """Keyword search over chunks. The raw user/model query is sanitized into
    an OR-of-terms FTS5 MATCH expression so arbitrary input can't break the parser."""
    terms = re.findall(r"[A-Za-z0-9]{2,}", query)
    if not terms:
        return []
    match_expr = " OR ".join(f'"{t}"' for t in terms[:20])
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT doc_id, title, content, bm25(chunks) AS score"
            " FROM chunks WHERE chunks MATCH ? ORDER BY score LIMIT ?",
            (match_expr, limit),
        ).fetchall()
        return [dict(r) for r in rows]
