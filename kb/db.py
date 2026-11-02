"""Database, masking and search for the knowledge base. Standard library only."""
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

KB_HOME = os.environ.get("KB_HOME", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.environ.get("KB_DB", os.path.join(KB_HOME, "data", "kb.sqlite"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,            -- item | segment | atom | raw
  source TEXT NOT NULL,          -- gmail | claude_code | notes ...
  source_id TEXT NOT NULL,       -- id of the original item
  ts TEXT NOT NULL,              -- ISO date
  title TEXT,
  content TEXT,
  metadata TEXT,                 -- JSON, has "url" or "path"
  superseded_by INTEGER
);
CREATE INDEX IF NOT EXISTS docs_src ON docs(source, source_id);
CREATE INDEX IF NOT EXISTS docs_ts ON docs(ts);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  title, content, content='docs', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO docs_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content);
END;
CREATE TRIGGER IF NOT EXISTS docs_au AFTER UPDATE ON docs BEGIN
  INSERT INTO docs_fts(docs_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content);
  INSERT INTO docs_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;
CREATE TABLE IF NOT EXISTS ingest_state (
  source TEXT NOT NULL, key TEXT NOT NULL, watermark TEXT,
  PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS ingest_log (
  id INTEGER PRIMARY KEY, ts TEXT, source TEXT, ok INTEGER, detail TEXT
);
"""


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- watermarks
def get_wm(con, source, key="default"):
    r = con.execute("SELECT watermark FROM ingest_state WHERE source=? AND key=?", (source, key)).fetchone()
    return r[0] if r else None


def set_wm(con, source, value, key="default"):
    con.execute(
        "INSERT INTO ingest_state(source,key,watermark) VALUES(?,?,?) "
        "ON CONFLICT(source,key) DO UPDATE SET watermark=excluded.watermark",
        (source, key, value),
    )
    con.commit()


# ---------------------------------------------------------------- masking
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
]
_KV_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|bearer)"
    r"(\s*[:=]\s*|\s+)(['\"]?)([^\s'\"]{6,})"
)


def mask(text):
    if not text:
        return text
    for p in _SECRET_PATTERNS:
        text = p.sub("[MASKED]", text)
    return _KV_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[MASKED]", text)


# ---------------------------------------------------------------- writing
RAW_CHUNK = 4000


def chunk(text, size=RAW_CHUNK):
    text = text or ""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def replace_rows(con, source, source_id, rows):
    """Delete every row of one item, then insert its new rows. Makes connectors idempotent."""
    con.execute("DELETE FROM docs WHERE source=? AND source_id=?", (source, source_id))
    for r in rows:
        con.execute(
            "INSERT INTO docs(kind,source,source_id,ts,title,content,metadata) VALUES(?,?,?,?,?,?,?)",
            (
                r["kind"], source, source_id, r["ts"],
                mask(r.get("title") or ""), mask(r.get("content") or ""),
                json.dumps(r.get("metadata") or {}),
            ),
        )
    con.commit()


def item_exists(con, source, source_id):
    return con.execute("SELECT 1 FROM docs WHERE source=? AND source_id=? LIMIT 1", (source, source_id)).fetchone() is not None


def log_ingest(con, source, ok, detail=""):
    con.execute("INSERT INTO ingest_log(ts,source,ok,detail) VALUES(?,?,?,?)", (now_iso(), source, int(ok), str(detail)[:2000]))
    con.commit()


# ---------------------------------------------------------------- search
_WORD = re.compile(r"[\w][\w.\-/]*", re.UNICODE)


def fts_query(q):
    words = [w.strip(".-/") for w in _WORD.findall(q)]
    words = [w.replace('"', "") for w in words if len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def _age_days(ts):
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - d).total_seconds() / 86400)
    except Exception:
        return 3650.0


def _ranked(con, match, raw, after, source, n):
    sql = (
        "SELECT d.id, d.kind, d.source, d.source_id, d.ts, d.title, d.metadata, d.superseded_by, "
        "snippet(docs_fts, 1, '[', ']', ' … ', 24) AS snip, bm25(docs_fts, 3.0, 1.0) AS score "
        "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid WHERE docs_fts MATCH ? "
    )
    args = [match]
    sql += "AND d.kind = 'raw' " if raw else "AND d.kind != 'raw' "
    if after:
        sql += "AND d.ts >= ? "
        args.append(after)
    if source:
        sql += "AND d.source = ? "
        args.append(source)
    sql += "ORDER BY score LIMIT ?"
    args.append(n)
    return con.execute(sql, args).fetchall()


def search(con, q, after=None, source=None, limit=10, k=60, raw_weight=0.5, half_life=180):
    match = fts_query(q)
    if not match:
        return []
    fused = {}
    for raw, weight in ((False, 1.0), (True, raw_weight)):
        for rank, row in enumerate(_ranked(con, match, raw, after, source, limit * 5)):
            # Group by original item so one email does not fill the whole list.
            key = (row["source"], row["source_id"])
            s = weight / (k + rank + 1)
            e = fused.setdefault(key, {"score": 0.0, "best": row, "best_s": -1})
            e["score"] += s
            if s > e["best_s"] and not (raw and e["best_s"] >= 0):
                e["best"], e["best_s"] = row, s
    out = []
    for key, e in fused.items():
        row = e["best"]
        score = e["score"] * (1 + 0.5 * math.exp(-_age_days(row["ts"]) / half_life))
        if row["superseded_by"]:
            score *= 0.5
        meta = json.loads(row["metadata"] or "{}")
        out.append({
            "score": round(score, 6), "ts": row["ts"], "source": row["source"], "kind": row["kind"],
            "source_id": row["source_id"], "title": row["title"], "snippet": row["snip"],
            "pointer": meta.get("url") or meta.get("path") or "",
        })
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:limit]


def stats(con):
    total = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    by = con.execute(
        "SELECT source, kind, COUNT(*) n, MIN(ts) first, MAX(ts) last FROM docs GROUP BY source, kind ORDER BY source, kind"
    ).fetchall()
    last = con.execute("SELECT MAX(ts) FROM ingest_log WHERE ok=1").fetchone()[0]
    errors = con.execute("SELECT ts, source, detail FROM ingest_log WHERE ok=0 ORDER BY id DESC LIMIT 5").fetchall()
    return {
        "rows": total,
        "last_ingest": last,
        "by_source": [dict(r) for r in by],
        "recent_errors": [dict(r) for r in errors],
    }


def header(con):
    s = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    last = con.execute("SELECT MAX(ts) FROM ingest_log WHERE ok=1").fetchone()[0]
    stale = ""
    if last and _age_days(last) > 0.5:
        stale = "  (STALE: last ingest over 12 hours ago, run ingest_all.py)"
    warn = ""
    for (src,) in con.execute("SELECT DISTINCT source FROM ingest_log").fetchall():
        r = con.execute("SELECT ok, detail FROM ingest_log WHERE source=? ORDER BY id DESC LIMIT 1", (src,)).fetchone()
        if r and not r[0]:
            hint = " (sign-in expired: run kb/gmail_auth.py again)" if src == "gmail" and "invalid_grant" in (r[1] or "") else ""
            warn += f"\n[kb] WARNING: last {src} ingest failed{hint}: {(r[1] or '')[:150]}"
    return f"[kb] {s} rows · last ingest {last or 'never'}{stale}{warn}"
