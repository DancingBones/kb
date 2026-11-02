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

from db import KB_HOME, chunk, env, get_wm, item_exists, replace_rows, set_wm

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
    t = unescape(re.sub(r"<[^>]+>", " ", h)).replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\s*\n\s*", "\n", t).strip()


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


# ====================================================================== Gmail over IMAP (app password)
def _msg_text(msg):
    plain, html = [], []
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ct = part.get_content_type()
        try:
            payload = part.get_content()
        except Exception:
            raw = part.get_payload(decode=True) or b""
            payload = raw.decode(part.get_content_charset() or "utf-8", "replace")
        if ct == "text/plain":
            plain.append(payload)
        elif ct == "text/html":
            html.append(payload)
    text = "\n".join(plain) if plain else _html_to_text("\n".join(html))
    text = re.split(r"\n(?:On .{5,120} wrote:|-{2,} ?Original Message ?-{2,})", text)[0]
    text = "\n".join(l for l in text.splitlines() if not l.startswith(">"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def gmail_imap(con, first_run_days=365, rerun_days=4):
    import email
    import imaplib
    from email import policy
    from email.header import decode_header, make_header

    user, pw = env("GMAIL_USER"), (env("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pw)
    try:
        folder = '"[Gmail]/All Mail"'
        typ, boxes = M.list()
        for b in boxes or []:
            line = b.decode(errors="replace")
            if "\\All" in line:
                folder = line.split(' "/" ')[-1]
                break
        typ, data = M.select(folder, readonly=True)  # read-only: never changes read/unread state
        if typ != "OK":
            raise RuntimeError(f"cannot open {folder}: {data}")
        validity = M.response("UIDVALIDITY")[1][0].decode()
        wm = get_wm(con, "gmail", "imap_uid")
        last_uid = int(wm.split(":")[1]) if wm and wm.split(":")[0] == validity else 0
        days = rerun_days if last_uid else first_run_days
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
        q = f"after:{since} -in:spam -in:trash -category:promotions -category:social"
        typ, data = M.uid("SEARCH", "X-GM-RAW", f'"{q}"')
        uids = [int(u) for u in (data[0] or b"").split()]
        new = [u for u in uids if u > last_uid]
        threads = set()
        for i in range(0, len(new), 500):
            batch = ",".join(str(u) for u in new[i:i + 500])
            typ, rows = M.uid("FETCH", batch, "(X-GM-THRID)")
            for r in rows or []:
                m = re.search(rb"X-GM-THRID (\d+)", r if isinstance(r, bytes) else r[0])
                if m:
                    threads.add(int(m.group(1)))
        added = skipped = 0
        for tid in threads:
            typ, data = M.uid("SEARCH", "X-GM-THRID", str(tid))
            t_uids = (data[0] or b"").split()[-50:]
            if not t_uids:
                continue
            try:
                typ, rows = M.uid("FETCH", b",".join(t_uids).decode(), "(BODY.PEEK[])")
            except imaplib.IMAP4.abort:
                raise
            except Exception as e:
                print(f"  thread {tid}: {e}")
                continue
            msgs = [email.message_from_bytes(r[1], policy=policy.default) for r in rows if isinstance(r, tuple)]
            if not msgs:
                continue
            msgs.sort(key=lambda m: _date(m))
            subject = str(make_header(decode_header(msgs[0].get("subject", "(no subject)"))))
            if SKIP_SUBJECT.search(subject):
                skipped += 1
                continue
            parts, people = [], set()
            for m in msgs:
                people.add(str(m.get("from", "")))
                parts.append(f"From: {m.get('from','')}\nTo: {m.get('to','')}\nDate: {_date(m)}\n\n{_msg_text(m)}")
            last_ts = _date(msgs[-1])
            text = f"Subject: {subject}\n\n" + "\n\n---\n\n".join(parts)
            sid = format(tid, "x")
            meta = {"url": f"https://mail.google.com/mail/u/0/#all/{sid}", "from": sorted(p for p in people if p)[:10],
                    "messages": len(msgs), "account": user}
            replace_rows(con, "gmail", sid, _rows_for("gmail", sid, last_ts, subject, text, meta))
            added += 1
        if uids:
            set_wm(con, "gmail", f"{validity}:{max(max(uids), last_uid)}", "imap_uid")
        set_wm(con, "gmail", datetime.now(timezone.utc).isoformat())
        return f"{added} threads updated, {skipped} security emails skipped (IMAP, read-only)"
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _date(m):
    try:
        return parsedate_to_datetime(m.get("date")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return "1970-01-01T00:00:00Z"


def gmail_any(con):
    if env("GMAIL_APP_PASSWORD"):
        return gmail_imap(con)
    if os.path.exists(os.path.join(SECRETS, "gmail_token.json")):
        return gmail(con)
    return "not set up (add GMAIL_USER and GMAIL_APP_PASSWORD to .env)"


# ================================================================= HighLevel
# One document per contact: their details, tags, opportunities, appointments,
# notes and every SMS / email / chat message, so a search for a client's name
# finds everything about them. Read-only: only GET requests are ever sent.
GHL_BASE = "https://services.leadconnectorhq.com"
GHL_FULL_EVERY_DAYS = 7          # rebuild every contact once a week (catches new notes)
GHL_MAX_MESSAGES = 2000          # per conversation


def _ghl_get(path, params=None, version="2021-07-28"):
    import time
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    tok = env("GHL_TOKEN")
    if not tok:
        raise RuntimeError("HighLevel not set up. Run: bash /opt/kb/deploy/set_ghl.sh")
    params = {k: v for k, v in (params or {}).items() if v is not None}
    url = GHL_BASE + path + ("?" + urlencode(params) if params else "")
    last = None
    for attempt in range(6):
        req = Request(url, headers={"Authorization": f"Bearer {tok}", "Version": version,
                                    "Accept": "application/json", "User-Agent": "kb-ingest/1"})
        try:
            with urlopen(req, timeout=60) as r:
                data = r.read()
            time.sleep(0.12)  # HighLevel allows 100 requests per 10 seconds
            return json.loads(data or b"{}")
        except HTTPError as e:
            body = e.read()[:300].decode(errors="replace")
            last = f"HighLevel {e.code} on {path}: {body}"
            if e.code == 429 or e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(last)
        except URLError as e:
            last = f"HighLevel network error on {path}: {e}"
            time.sleep(2 ** attempt)
    raise RuntimeError(last)


def _ghl_pages(path, params, key, limit=100):
    """Cursor pages (meta.startAfterId / meta.startAfter), as /contacts/ and /opportunities/search use."""
    params = dict(params, limit=limit)
    seen = set()
    while True:
        d = _ghl_get(path, params)
        items = d.get(key) or []
        fresh = [x for x in items if x.get("id") not in seen]
        if not fresh:
            return
        for x in fresh:
            seen.add(x.get("id"))
            yield x
        meta = d.get("meta") or {}
        if len(items) < limit or not meta.get("startAfterId"):
            return
        params = dict(params, startAfterId=meta["startAfterId"], startAfter=meta.get("startAfter"))


def _ghl_ts(v):
    """HighLevel dates come as epoch milliseconds or ISO strings. Returns ISO UTC or ''."""
    if v in (None, ""):
        return ""
    try:
        if isinstance(v, (int, float)) or str(v).isdigit():
            return datetime.fromtimestamp(int(v) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ""


def _ghl_conversations(loc, contact_id):
    d = _ghl_get("/conversations/search", {"locationId": loc, "contactId": contact_id, "limit": 100})
    return d.get("conversations") or []


def _ghl_messages(conv_id):
    out, last_id = [], None
    while len(out) < GHL_MAX_MESSAGES:
        d = (_ghl_get(f"/conversations/{conv_id}/messages", {"limit": 100, "lastMessageId": last_id})
             .get("messages") or {})
        msgs = d.get("messages") or []
        if not msgs:
            break
        out.extend(msgs)
        new_last = d.get("lastMessageId") or msgs[-1].get("id")
        if not d.get("nextPage") or new_last == last_id:
            break
        last_id = new_last
    return out


def _ghl_recent_conversation_contacts(loc, since_iso):
    """Contact ids whose conversations had a message after since_iso (newest first, stop when older)."""
    ids, after, seen = set(), None, set()
    while True:
        d = _ghl_get("/conversations/search", {"locationId": loc, "limit": 100, "sort": "desc",
                                               "sortBy": "last_message_date", "startAfterDate": after})
        convs = [c for c in d.get("conversations") or [] if c.get("id") not in seen]
        if not convs:
            return ids
        for c in convs:
            seen.add(c.get("id"))
            if _ghl_ts(c.get("lastMessageDate")) < since_iso:
                return ids
            if c.get("contactId"):
                ids.add(c["contactId"])
        after = convs[-1].get("lastMessageDate")
        if not after or len(seen) > 20000:
            return ids


def _ghl_contact_text(c, opps, appts, notes_, convs, stages):
    name = (c.get("contactName") or " ".join(x for x in (c.get("firstName"), c.get("lastName")) if x)
            or c.get("companyName") or c.get("email") or c.get("phone") or "(no name)").strip()
    lines = [f"HighLevel contact: {name}"]
    for label, k in (("Company", "companyName"), ("Email", "email"), ("Phone", "phone"),
                     ("Source", "source"), ("Type", "type")):
        if c.get(k):
            lines.append(f"{label}: {c[k]}")
    addr = ", ".join(x for x in (c.get("address1"), c.get("city"), c.get("state"), c.get("postalCode")) if x)
    if addr:
        lines.append(f"Address: {addr}")
    if c.get("tags"):
        lines.append("Tags: " + ", ".join(c["tags"]))
    if c.get("dateAdded"):
        lines.append(f"Added: {_ghl_ts(c['dateAdded'])[:10]}")
    if opps:
        lines.append("\nOpportunities:")
        for o in opps:
            val = f", ${o['monetaryValue']}" if o.get("monetaryValue") else ""
            stage = stages.get(o.get("pipelineStageId"), "")
            lines.append(f"- {o.get('name') or '(unnamed)'}: {o.get('status', '')}"
                         f"{' / ' + stage if stage else ''}{val} (created {_ghl_ts(o.get('createdAt'))[:10]})")
    if appts:
        lines.append("\nAppointments:")
        for a in sorted(appts, key=lambda a: _ghl_ts(a.get("startTime"))):
            lines.append(f"- {_ghl_ts(a.get('startTime'))[:16].replace('T', ' ')} {a.get('title') or ''} "
                         f"[{a.get('appointmentStatus') or a.get('status') or ''}] {a.get('_calendar', '')}".rstrip())
    if notes_:
        lines.append("\nNotes:")
        for n in sorted(notes_, key=lambda n: n.get("dateAdded") or ""):
            lines.append(f"- {_ghl_ts(n.get('dateAdded'))[:10]}: {_html_to_text(n.get('body') or '')}")
    for conv, msgs in convs:
        if not msgs:
            continue
        lines.append("\nMessages:")
        for m in sorted(msgs, key=lambda m: _ghl_ts(m.get("dateAdded"))):
            kind = str(m.get("messageType") or m.get("type") or "").replace("TYPE_", "").lower()
            body = m.get("body") or ""
            if "<" in body and ">" in body:
                body = _html_to_text(body)
            if not body and kind == "call":
                body = f"(call, {m.get('status') or ''})"
            if not body:
                continue
            who = "them" if m.get("direction") == "inbound" else "us"
            lines.append(f"[{_ghl_ts(m.get('dateAdded'))[:16].replace('T', ' ')}] {kind} {who}: {body.strip()}")
    return name, "\n".join(lines)


def ghl(con):
    loc = env("GHL_LOCATION_ID")
    if not env("GHL_TOKEN") or not loc:
        return "not set up (run deploy/set_ghl.sh)"
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    wm, full_wm = get_wm(con, "ghl"), get_wm(con, "ghl", "full")
    full = not wm or not full_wm or full_wm < (datetime.now(timezone.utc) - timedelta(days=GHL_FULL_EVERY_DAYS)
                                                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
    warnings = []

    contacts = {c["id"]: c for c in _ghl_pages("/contacts/", {"locationId": loc}, "contacts")}

    stages = {}
    try:
        for p in _ghl_get("/opportunities/pipelines", {"locationId": loc}).get("pipelines") or []:
            for s in p.get("stages") or []:
                stages[s.get("id")] = f"{p.get('name', '')}: {s.get('name', '')}"
    except Exception as e:
        warnings.append(f"pipelines: {e}")
    opps = {}
    try:
        for o in _ghl_pages("/opportunities/search", {"location_id": loc, "status": "all"}, "opportunities"):
            opps.setdefault(o.get("contactId"), []).append(o)
    except Exception as e:
        warnings.append(f"opportunities: {e}")

    appts = {}
    try:
        now = datetime.now(timezone.utc)
        start = int((now - timedelta(days=365)).timestamp() * 1000)
        end = int((now + timedelta(days=120)).timestamp() * 1000)
        month = 31 * 86400 * 1000  # HighLevel answers event lists best one month at a time
        for cal in _ghl_get("/calendars/", {"locationId": loc}, "2021-04-15").get("calendars") or []:
            seen_ev = set()
            for t in range(start, end, month):
                evs = _ghl_get("/calendars/events", {"locationId": loc, "calendarId": cal["id"], "startTime": t,
                                                     "endTime": min(t + month, end) - 1}, "2021-04-15").get("events") or []
                for ev in evs:
                    if ev.get("deleted") or ev.get("id") in seen_ev:
                        continue
                    seen_ev.add(ev.get("id"))
                    ev["_calendar"] = cal.get("name", "")
                    appts.setdefault(ev.get("contactId"), []).append(ev)
    except Exception as e:
        warnings.append(f"appointments: {e}")

    if full:
        todo = set(contacts)
    else:
        todo = {cid for cid, c in contacts.items() if _ghl_ts(c.get("dateUpdated") or c.get("dateAdded")) >= wm}
        todo |= {cid for cid, os_ in opps.items() if any(_ghl_ts(o.get("updatedAt")) >= wm for o in os_)}
        todo |= {cid for cid, es in appts.items() if any(_ghl_ts(e.get("dateUpdated")) >= wm for e in es)}
        try:
            todo |= _ghl_recent_conversation_contacts(loc, wm)
        except Exception as e:
            warnings.append(f"recent conversations: {e}")
        todo = {cid for cid in todo if cid in contacts}

    done = failed = 0
    for cid in todo:
        c = contacts[cid]
        try:
            try:
                notes_ = _ghl_get(f"/contacts/{cid}/notes").get("notes") or []
            except RuntimeError as e:
                notes_ = []
                if ("401" in str(e) or "403" in str(e)) and "notes: no access" not in warnings:
                    warnings.append("notes: no access")
            convs = []
            for conv in _ghl_conversations(loc, cid):
                convs.append((conv, _ghl_messages(conv["id"])))
            name, text = _ghl_contact_text(c, opps.get(cid, []), appts.get(cid, []), notes_, convs, stages)
            dates = [_ghl_ts(c.get("dateAdded")), _ghl_ts(c.get("dateUpdated"))]
            dates += [_ghl_ts(conv.get("lastMessageDate")) for conv, _ in convs]
            ts = max(d for d in dates if d) if any(dates) else started
            meta = {"url": f"https://app.gohighlevel.com/v2/location/{loc}/contacts/detail/{cid}",
                    "email": c.get("email"), "phone": c.get("phone")}
            replace_rows(con, "ghl", cid, _rows_for("ghl", cid, ts, f"{name} (HighLevel)", text, meta))
            done += 1
        except Exception as e:
            failed += 1
            print(f"  ghl contact {cid}: {type(e).__name__}: {e}")
            if failed >= 20 and done == 0:
                raise
    gone = [r[0] for r in con.execute("SELECT DISTINCT source_id FROM docs WHERE source='ghl'")
            if r[0] not in contacts]
    if full:  # contacts deleted in HighLevel leave the knowledge base on the weekly rebuild
        for sid in gone:
            replace_rows(con, "ghl", sid, [])
        set_wm(con, "ghl", started, "full")
    set_wm(con, "ghl", started)
    msg = (f"{done} contacts {'rebuilt (full)' if full else 'updated'} of {len(contacts)}, "
           f"{failed} failed, {sum(map(len, opps.values()))} opportunities, "
           f"{sum(map(len, appts.values()))} appointments")
    if warnings:
        msg += "; " + "; ".join(warnings[:5])
    return msg


CONNECTORS = {"gmail": gmail_any, "claude_code": claude_code, "notes": notes, "ghl": ghl}
