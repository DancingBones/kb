#!/usr/bin/env bash
# Asks for the Gmail address and app password, saves them to /opt/kb/.env,
# checks the login, then starts the first Gmail pull in the background.
set -euo pipefail
ENVF=/opt/kb/.env
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
printf '\033[?2004l'   # turn off bracketed paste so pasted text arrives clean
read -r -p "Gmail address to index: " U
read -r -s -p "App password (16 letters, paste with right-click, it stays hidden): " P; echo
U=$(printf '%s' "$U" | sed 's/\x1b\[[0-9]*~//g' | tr -d '[:space:]~')
P=$(printf '%s' "$P" | sed 's/\x1b\[[0-9]*~//g' | tr -cd 'A-Za-z')
[[ "$U" == *@* ]] || { echo "That doesn't look like an email address."; exit 1; }
# A paste that landed twice gives 32 letters: keep one copy.
if [ ${#P} -eq 32 ] && [ "${P:0:16}" = "${P:16:16}" ]; then P=${P:0:16}; fi
[ ${#P} -eq 16 ] || { echo "App password should be 16 letters, got ${#P}. Try again."; exit 1; }
sed -i '/^GMAIL_USER=/d;/^GMAIL_APP_PASSWORD=/d' "$ENVF"
printf 'GMAIL_USER=%s\nGMAIL_APP_PASSWORD=%s\n' "$U" "$P" >> "$ENVF"
chown kb:kb "$ENVF"; chmod 600 "$ENVF"
echo "Saved. Checking the login..."
if sudo -u kb KB_HOME=/opt/kb /opt/kb/venv/bin/python -c "
import imaplib,sys; sys.path.insert(0,'/opt/kb/kb'); from db import env
M=imaplib.IMAP4_SSL('imap.gmail.com'); M.login(env('GMAIL_USER'), env('GMAIL_APP_PASSWORD')); M.logout()"; then
  echo "Gmail login works."
else
  echo "Gmail rejected the login. Check the address and create a new app password, then run this again."; exit 1
fi
sudo -u kb nohup bash -c 'cd /opt/kb && KB_HOME=/opt/kb venv/bin/python kb/ingest_all.py' >> /opt/kb/data/logs/ingest.log 2>&1 &
echo "First Gmail pull started in the background (12 months, can take a while)."
echo "Check progress any time with:  tail -5 /opt/kb/data/logs/ingest.log"
