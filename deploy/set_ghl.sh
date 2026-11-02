#!/usr/bin/env bash
# Asks for the HighLevel Location ID and Private Integration token, saves them to /opt/kb/.env,
# checks what the token can read, then starts the first HighLevel pull in the background.
set -euo pipefail
ENVF=/opt/kb/.env
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
printf '\033[?2004l'   # turn off bracketed paste so pasted text arrives clean
read -r -p "HighLevel Location ID (or paste any address from your HighLevel tab): " L
read -r -s -p "Private Integration token (starts with pit-, paste with right-click, it stays hidden): " T; echo
clean() { printf '%s' "$1" | sed 's/\x1b\[[0-9]*~//g' | tr -d '[:space:]~'; }
L=$(clean "$L"); T=$(clean "$T")
# An address like https://app.gohighlevel.com/v2/location/AbC123/dashboard holds the ID after /location/
if [[ "$L" == */location/* ]]; then L=$(printf '%s' "$L" | sed -E 's#.*/location/([A-Za-z0-9]+).*#\1#'); fi
[[ "$L" =~ ^[A-Za-z0-9]{10,40}$ ]] || { echo "That doesn't look like a Location ID."; exit 1; }
# A paste that landed twice: keep the first token.
if [[ "$T" == pit-*pit-* ]]; then T="pit-${T#pit-}"; REST="${T#pit-}"; T="pit-${REST%%pit-*}"; fi
[[ "$T" == pit-* ]] || { echo "The token should start with pit-. Copy it again from HighLevel."; exit 1; }
sed -i '/^GHL_TOKEN=/d;/^GHL_LOCATION_ID=/d' "$ENVF"
printf 'GHL_LOCATION_ID=%s\nGHL_TOKEN=%s\n' "$L" "$T" >> "$ENVF"
chown kb:kb "$ENVF"; chmod 600 "$ENVF"
echo "Saved. Checking what the token can read..."
sudo -u kb KB_HOME=/opt/kb /opt/kb/venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "/opt/kb/kb")
from db import env
from connectors import _ghl_get
loc = env("GHL_LOCATION_ID")
checks = [("Contacts and notes", "/contacts/", {"locationId": loc, "limit": 1}),
          ("Conversations and messages", "/conversations/search", {"locationId": loc, "limit": 1}),
          ("Opportunities", "/opportunities/search", {"location_id": loc, "limit": 1}),
          ("Calendars", "/calendars/", {"locationId": loc}, "2021-04-15")]
bad = 0
for label, path, p, *v in checks:
    try:
        _ghl_get(path, p, *v); print(f"  OK       {label}")
    except Exception as e:
        bad += 1; print(f"  MISSING  {label}: {str(e)[:120]}")
if bad == len(checks):
    sys.exit("HighLevel refused every request. Check the Location ID and the token, then run this again.")
if bad:
    print("Some parts are missing: edit the Private Integration in HighLevel, tick the read-only scopes listed, and run this again.")
PY
sudo -u kb nohup bash -c 'cd /opt/kb && KB_HOME=/opt/kb venv/bin/python kb/ingest_all.py ghl' >> /opt/kb/data/logs/ingest.log 2>&1 &
echo "First HighLevel pull started in the background (every contact, can take 30 minutes or more)."
echo "Check progress any time with:  grep ghl /opt/kb/data/logs/ingest.log | tail -5"
