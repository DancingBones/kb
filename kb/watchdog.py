"""Hourly health check. Emails Ty when something stops working, once a day while it stays broken,
and once more when it is fixed. Run as root from cron (it also checks the API container and disk).

  python kb/watchdog.py           check, email only if something changed
  python kb/watchdog.py --show    print the check, send nothing
  python kb/watchdog.py --test    send a test email
"""
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import KB_HOME, connect, env  # noqa: E402

DATA = os.path.join(KB_HOME, "data")
STATE = os.path.join(DATA, "watchdog.json")
STALE_HOURS = 13          # ingest runs every 6 hours, so two missed runs
PC_QUIET_DAYS = 7         # a computer that has sent no Claude Code session for a week
RESEND_HOURS = 24


def _hours_since(iso):
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def _give_back_to_kb():
    """Running as root may create the database's -wal/-shm files; the kb user must keep owning them."""
    try:
        import pwd
        kb = pwd.getpwnam("kb")
    except Exception:
        return
    from db import DB_PATH
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH + suffix
        if os.path.exists(p) and os.stat(p).st_uid != kb.pw_uid:
            os.chown(p, kb.pw_uid, kb.pw_gid)


def check():
    problems, notes = [], []
    con = connect()
    for src, label in (("gmail", "Gmail"), ("ghl", "HighLevel"), ("claude_code", "Claude Code sessions"),
                       ("summaries", "Summaries")):
        last = con.execute("SELECT ts, ok, detail FROM ingest_log WHERE source=? ORDER BY id DESC LIMIT 1", (src,)).fetchone()
        if not last:
            continue  # never run yet (first run still going, or not set up)
        detail = last["detail"] or ""
        if detail.startswith("not set up") or detail.startswith("skipped"):
            continue
        if not last["ok"]:
            hint = ""
            if src == "gmail" and re.search(r"(?i)authenticationfailed|invalid credentials|application-specific", detail):
                hint = " The Gmail app password was probably revoked: make a new one and run bash /opt/kb/deploy/set_gmail.sh"
            if src == "ghl" and re.search(r"\b40[13]\b", detail):
                hint = " The HighLevel token was probably deleted or lost a permission: run bash /opt/kb/deploy/set_ghl.sh with a new one"
            problems.append(f"{label} failed at {last['ts'][:16].replace('T', ' ')} UTC: {detail[:200]}.{hint}")
        else:
            ok = con.execute("SELECT MAX(ts) FROM ingest_log WHERE source=? AND ok=1", (src,)).fetchone()[0]
            if ok and _hours_since(ok) > STALE_HOURS:
                problems.append(f"{label} has not run for {int(_hours_since(ok))} hours (it should run every 6).")
            m = re.match(r"(\d+) items summarized, (\d+) failed", detail)
            if src == "summaries" and m and int(m.group(1)) == 0 and int(m.group(2)) > 0:
                problems.append("Summaries: every item failed in the last run. The Claude sign-in on the server has "
                                "probably expired: run bash /opt/kb/deploy/set_claude.sh")
            if src == "ghl" and "; " in detail:
                notes.append("HighLevel: " + detail.split("; ", 1)[1][:200])
    if not con.execute("SELECT 1 FROM ingest_log WHERE ok=1 LIMIT 1").fetchone():
        log = os.path.join(DATA, "logs", "ingest.log")
        if os.path.exists(log) and time.time() - os.path.getmtime(log) > 3 * 3600:
            problems.append("No ingest has ever finished and the log has been quiet for 3 hours. "
                            "Look at: tail -50 /opt/kb/data/logs/ingest.log")
    con.close()
    _give_back_to_kb()

    inbox = os.path.join(DATA, "inbox", "claude_code")
    if os.path.isdir(inbox):
        for machine in sorted(os.listdir(inbox)):
            newest = 0
            for root, _, files in os.walk(os.path.join(inbox, machine)):
                for f in files:
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            if newest and (time.time() - newest) / 86400 > PC_QUIET_DAYS:
                notes.append(f"Computer {machine} has sent no Claude Code sessions for {int((time.time() - newest) / 86400)} days "
                             "(fine if you haven't used Claude Code there; otherwise check the KB Sync task).")

    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "kb-api"], capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip() != "true":
            problems.append("The search API container kb-api is not running, so Claude cannot search. "
                            "Fix: cd /opt/kb && bash deploy/install.sh")
    except Exception:
        pass
    free_gb = shutil.disk_usage(DATA).free / 1e9
    if free_gb < 2:
        problems.append(f"The server has only {free_gb:.1f} GB of disk left.")
    return problems, notes


def send(subject, body):
    user, pw = env("GMAIL_USER"), (env("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = env("KB_ALERT_EMAIL") or user
    if not (user and pw and to):
        raise RuntimeError("no GMAIL_USER / GMAIL_APP_PASSWORD in .env to send alerts with")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body + "\n\n(Sent by the knowledge base watchdog on your Hostinger server.)")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)


def main():
    if "--test" in sys.argv:
        send("[KB] Test alert", "This is a test. If you can read it, knowledge base alerts reach you.")
        print("test email sent")
        return
    problems, notes = check()
    if "--show" in sys.argv:
        print("\n".join(["PROBLEMS:"] + problems + ["NOTES:"] + notes) if problems or notes else "all good")
        return
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
    key = sorted(re.sub(r"\d+", "#", p) for p in problems)  # same problem with a new hour count is not "new"
    now = time.time()
    if problems and (key != state.get("key") or now - state.get("sent", 0) > RESEND_HOURS * 3600):
        body = "Something in your knowledge base needs a look:\n\n- " + "\n- ".join(problems)
        if notes:
            body += "\n\nAlso:\n- " + "\n- ".join(notes)
        send(f"[KB] {len(problems)} problem{'s' if len(problems) > 1 else ''}: {problems[0][:60]}", body)
        state = {"key": key, "sent": now}
    elif not problems and state.get("key"):
        send("[KB] All working again", "Everything in your knowledge base is running normally again.")
        state = {"key": [], "sent": now}
    else:
        state["key"] = key
    with open(STATE, "w") as f:
        json.dump(state, f)
    print(f"watchdog {time.strftime('%Y-%m-%d %H:%M')}: {len(problems)} problems, {len(notes)} notes")


if __name__ == "__main__":
    main()
