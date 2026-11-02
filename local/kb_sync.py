"""Upload new or changed Claude Code sessions from this computer to the knowledge base server.

Setup: create ~/.kb_sync.env with two lines
    KB_URL=https://<your-server>/kb
    KB_TOKEN=<the token the installer printed>
Optionally add  KB_NOTES_DIR=/path/to/notes  to also upload .md/.txt notes.

Run it by hand or on a schedule (see README). Standard library only, Python 3.8+."""
import json
import os
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".kb_sync_state.json"


def load_env():
    env = {}
    f = HOME / ".kb_sync.env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
    env.update({k: v for k, v in os.environ.items() if k.startswith("KB_")})
    return env


def safe(s):
    return "".join(c if c.isalnum() or c in "._-@+ " else "_" for c in s)[:200] or "_"


def upload(url, token, source, rel_parts, path):
    data = path.read_bytes()
    dest = "/".join(urllib.parse.quote(safe(p)) for p in rel_parts)
    req = urllib.request.Request(f"{url}/upload/{source}/{dest}", data=data, method="PUT",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.status == 200


def main():
    env = load_env()
    url, token = env.get("KB_URL", "").rstrip("/"), env.get("KB_TOKEN", "")
    if not url or not token:
        sys.exit("Set KB_URL and KB_TOKEN in ~/.kb_sync.env")
    machine = safe(env.get("KB_MACHINE") or socket.gethostname().split(".")[0])
    state = json.loads(STATE.read_text()) if STATE.exists() else {}

    jobs = []
    projects = Path(env.get("KB_CLAUDE_DIR") or HOME / ".claude" / "projects")
    if projects.exists():
        for p in projects.glob("*/*.jsonl"):
            jobs.append(("claude_code", [machine, p.parent.name, p.name], p))
    notes = env.get("KB_NOTES_DIR")
    if notes and Path(notes).exists():
        for p in Path(notes).rglob("*"):
            if p.is_file() and p.suffix.lower() in (".md", ".txt", ".vtt", ".srt"):
                rel = p.relative_to(notes).parts
                jobs.append(("notes", [machine, *rel], p))

    sent = failed = 0
    for source, rel, p in jobs:
        st = p.stat()
        key = f"{source}:{p}"
        sig = f"{st.st_size}:{int(st.st_mtime)}"
        if state.get(key) == sig:
            continue
        try:
            if upload(url, token, source, rel, p):
                state[key] = sig
                sent += 1
        except Exception as e:
            failed += 1
            print(f"failed {p.name}: {e}", file=sys.stderr)
    STATE.write_text(json.dumps(state))
    print(f"kb_sync: {sent} files uploaded, {failed} failed, {len(jobs)} checked")


if __name__ == "__main__":
    main()
