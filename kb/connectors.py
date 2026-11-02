"""One function per source. Each pulls only what is new, stamps a date, is safe to run twice,
and keeps a pointer to the original in metadata."""
import base64
import glob
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape

from db import KB_HOME, chunk, get_wm, item_exists, replace_rows, set_wm

DATA = os.path.join(KB_HOME, "data")
SECRETS = os.path.join(DATA, "secrets")
INBOX = os.path.join(DATA, "inbox")
LONG_ITEM = 6000  # characters; longer items get Haiku summaries


def _rows_for(source, source_id, ts, title, text, meta, short_prefix=""):
    """Build the standard rows: one 'item' row (title + start of text) and the raw 4,000-char chunks."""
    long_item = len(text) > LONG_ITEM
    meta = dict(meta, needs_summary=long_item, chars=len(text))
    rows = [{"kind": "item", "ts": ts, "title": title,
             "content": short_prefix + (text[:1500] if long_item else text), "metadata": meta}]
    if long_item:
        for i, c in enumerate(chunk(text)):
            rows.append({"kind": "raw", "ts": ts, "title": f"{title} (part {i + 1})", "content": c,
                         "metadata": dict(meta, chunk=i, needs_summary=False)})
    return rows


# ====================================================================== Gmail
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SKIP_SUBJECT = re.compile(
    r"(?i)(password reset|reset your password|reset password|verification code|verify your|one[- ]time (pass)?code|"
    r"\botp\b|2fa|two[- ]factor|2-step|security code|login code|sign[- ]in code|confirm your (email|account)|magic link)"
)


def _gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    tok = os.path.join(SECRETS, "gmail_token.json")
    if not os.path.exists(tok):
        raise RuntimeError("Gmail not authorized yet. Run: kb/venv/bin/python kb/gmail_auth.py")
    creds = Credentials.from_authorized_user_file(tok, GMAIL_SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        with open(tok, "w") as f:
            f.write(creds.to_json())
        os.chmod(tok, 0o600)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _html_to_text(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
    return unescape(re.sub(r"<[^>]+>", " ", h))


def _body(payload):
    plain, html = [], []

    def walk(p):
        mt = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mt == "text/plain":
            plain.append(_decode(data))
        elif data and mt == "text/html":
            html.append(_decode(data))
        for c in p.get("parts", []) or []:
            walk(c)

    walk(payload)
    text = "\n".join(plain) if plain else _html_to_text("\n".join(html))
    # Drop quoted replies, they repeat earlier messages in the thread.
    text = re.split(r"\n(?:On .{5,120} wrote:|-{2,} ?Original Message ?-{2,})", text)[0]
    text = "\n".join(l for l in text.splitlines() if not l.startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def gmail(con, first_run_days=365, rerun_days=4, max_threads=None):
    svc = _gmail_service()
    wm = get_wm(con, "gmail")
    days = rerun_days if wm else first_run_days
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    q = f"after:{since} -in:spam -in:trash -category:promotions -category:social"
    added = skipped = 0
    page = None
    while True:
        resp = svc.users().threads().list(userId="me", q=q, maxResults=100, pageToken=page).execute()
        for t in resp.get("threads", []):
            tid = t["id"]
            if wm and item_exists(con, "gmail", tid) and t.get("historyId") and _same_history(con, tid, t["historyId"]):
                continue
            th = svc.users().threads().get(userId="me", id=tid, format="full").execute()
            msgs = th.get("messages", [])
            if not msgs:
                continue
            hdr = {h["name"].lower(): h["value"] for h in msgs[0]["payload"].get("headers", [])}
            subject = hdr.get("subject", "(no subject)")
            if SKIP_SUBJECT.search(subject):
                skipped += 1
                continue
            parts, people, last_ts = [], set(), None
            for m in msgs:
                h = {x["name"].lower(): x["value"] for x in m["payload"].get("headers", [])}
                try:
                    d = parsedate_to_datetime(h.get("date"))
                    last_ts = d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception:
                    last_ts = datetime.fromtimestamp(int(m["internalDate"]) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                people.add(h.get("from", ""))
                parts.append(f"From: {h.get('from','')}\nTo: {h.get('to','')}\nDate: {last_ts}\n\n{_body(m['payload'])}")
            text = f"Subject: {subject}\n\n" + "\n\n---\n\n".join(parts)
            meta = {"url": f"https://mail.google.com/mail/u/0/#all/{tid}", "from": sorted(p for p in people if p)[:10],
                    "messages": len(msgs), "history_id": th.get("historyId"), "labels": msgs[-1].get("labelIds", [])}
            replace_rows(con, "gmail", tid, _rows_for("gmail", tid, last_ts, subject, text, meta))
            added += 1
            if max_threads and added >= max_threads:
                break
        page = resp.get("nextPageToken")
        if not page or (max_threads and added >= max_threads):
            break
    set_wm(con, "gmail", datetime.now(timezone.utc).isoformat())
    return f"{added} threads updated, {skipped} security emails skipped"


def _same_history(con, tid, history_id):
    r = con.execute("SELECT metadata FROM docs WHERE source='gmail' AND source_id=? AND kind='item'", (tid,)).fetchone()
    return bool(r) and json.loads(r[0]).get("history_id") == history_id


# ============================================================ Claude Code sessions
def _session_text(path):
    lines, first_ts, last_ts, cwd, title = [], None, None, None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                e = json.loads(raw)
            except Exception:
                continue
            if e.get("type") == "summary" and e.get("summary"):
                title = title or e["summary"]
            if e.get("type") not in ("user", "assistant"):
                continue
            ts = e.get("timestamp")
            first_ts = first_ts or ts
            last_ts = ts or last_ts
            cwd = cwd or e.get("cwd")
            msg = e.get("message") or {}
            content = msg.get("content")
            out = []
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                for b in content:
                    t = b.get("type")
                    if t == "text":
                        out.append(b.get("text", ""))
                    elif t == "tool_use":
                        inp = b.get("input") or {}
                        cmd = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
                        out.append(f"[tool {b.get('name')}] {str(cmd)[:500]}")
                    elif t == "tool_result":
                        c = b.get("content")
                        if isinstance(c, list):
                            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                        c = str(c or "")
                        if b.get("is_error") or len(c) < 1200:
                            out.append(f"[result] {c[:1200]}")
            text = "\n".join(x for x in out if x and x.strip())
            if text:
                role = "USER" if e["type"] == "user" else "CLAUDE"
                lines.append(f"{role}: {text}")
    return "\n\n".join(lines), first_ts, last_ts, cwd, title


def claude_code(con):
    """Reads session files uploaded by local/kb_sync.py into data/inbox/claude_code/<machine>/<project>/<id>.jsonl"""
    root = os.path.join(INBOX, "claude_code")
    added = 0
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        rel = os.path.relpath(path, root)
        st = os.stat(path)
        key = f"{st.st_size}:{int(st.st_mtime)}"
        if get_wm(con, "claude_code", rel) == key:
            continue
        text, first_ts, last_ts, cwd, title = _session_text(path)
        if not text.strip():
            set_wm(con, "claude_code", key, rel)
            continue
        first_user = next((l[6:] for l in text.split("\n\n") if l.startswith("USER: ")), "")
        title = title or (first_user[:90].replace("\n", " ") or os.path.basename(path))
        ts = last_ts or datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        meta = {"path": rel, "cwd": cwd, "started": first_ts}
        sid = rel[:-6]
        replace_rows(con, "claude_code", sid, _rows_for("claude_code", sid, ts, title, text, meta, f"Project: {cwd}\n" if cwd else ""))
        set_wm(con, "claude_code", key, rel)
        added += 1
    return f"{added} sessions updated"


# ============================================================ Notes / text files
def notes(con):
    """Any .md or .txt files uploaded into data/inbox/notes (optional source)."""
    root = os.path.join(INBOX, "notes")
    added = 0
    for path in glob.glob(os.path.join(root, "**", "*.*"), recursive=True):
        if not path.lower().endswith((".md", ".txt", ".vtt", ".srt")):
            continue
        rel = os.path.relpath(path, root)
        st = os.stat(path)
        key = f"{st.st_size}:{int(st.st_mtime)}"
        if get_wm(con, "notes", rel) == key:
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        ts = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        replace_rows(con, "notes", rel, _rows_for("notes", rel, ts, os.path.basename(path), text, {"path": rel}))
        set_wm(con, "notes", key, rel)
        added += 1
    return f"{added} files updated"


CONNECTORS = {"gmail": gmail, "claude_code": claude_code, "notes": notes}
