#!/usr/bin/env bash
# Installs or updates the knowledge base on an Ubuntu VPS. Safe to run again.
# Run as root from the repo checkout:  bash /opt/kb/deploy/install.sh
set -euo pipefail

KB=/opt/kb
DATA=$KB/data
ENVF=$KB/.env
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }
[ -f "$KB/kb/db.py" ] || { echo "Expected the repo at $KB"; exit 1; }

echo "== 1/6 system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip sqlite3 git openssl cron >/dev/null

echo "== 2/6 service user and folders"
id kb >/dev/null 2>&1 || useradd --system --home "$KB" --shell /usr/sbin/nologin kb
mkdir -p "$DATA/secrets" "$DATA/inbox/claude_code" "$DATA/inbox/notes" "$DATA/logs" "$DATA/backups"
chown -R kb:kb "$DATA"
chmod 700 "$DATA" "$DATA/secrets"

echo "== 3/6 python environment"
[ -d "$KB/venv" ] || python3 -m venv "$KB/venv"
"$KB/venv/bin/pip" install -q --upgrade pip
"$KB/venv/bin/pip" install -q -r "$KB/kb/requirements.txt"
"$KB/venv/bin/python" -c "import sqlite3;sqlite3.connect(':memory:').execute('create virtual table t using fts5(x)')" \
  || { echo "SQLite on this system has no FTS5"; exit 1; }

echo "== 4/6 secrets file"
if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<EOF
# Knowledge base settings. Never commit this file.
KB_TOKEN=$(openssl rand -hex 32)
ANTHROPIC_API_KEY=
# Turn on after you approve the summary backfill cost (python kb/summarize.py --estimate)
KB_AUTO_SUMMARIZE=0
EOF
fi
chown kb:kb "$ENVF"; chmod 600 "$ENVF"
TOKEN=$(grep '^KB_TOKEN=' "$ENVF" | cut -d= -f2)
sudo -u kb KB_HOME=$KB "$KB/venv/bin/python" "$KB/kb/kb.py" init

echo "== 5/6 search API container"
command -v docker >/dev/null || { echo "Docker not found. This installer expects the Hostinger n8n image (Docker + Traefik)."; exit 1; }
N8N=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /n8n/ {print $1; exit}')
TRAEFIK=$(docker ps --format '{{.Names}} {{.Image}}' | awk 'tolower($2) ~ /traefik/ {print $1; exit}')
LABELS=()
NET_ARGS=(-p 127.0.0.1:8765:8765)
PUBLIC_URL="(not public: no Traefik found, API only on 127.0.0.1:8765)"
if [ -n "$N8N" ] && [ -n "$TRAEFIK" ]; then
  ALL=$(docker inspect "$N8N" --format '{{range $k,$v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}')
  RULE=$(echo "$ALL" | grep -E '^traefik\.http\.routers\.[^.]+\.rule=' | head -1 | cut -d= -f2-)
  HOST=$(echo "$RULE" | sed -nE 's/.*Host\(`([^`]+)`\).*/\1/p')
  RESOLVER=$(echo "$ALL" | grep -E '\.tls\.certresolver=' | head -1 | cut -d= -f2-)
  ENTRY=$(echo "$ALL" | grep -E '\.entrypoints=' | grep -v '=web$' | head -1 | cut -d= -f2-)
  NET=$(docker inspect "$TRAEFIK" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' | awk '{print $1}')
  # The knowledge base gets its own hostname (the VPS name without "n8n."), so Claude's
  # connector setup never finds n8n's sign-in page. Override with KB_HOST=... if needed.
  KB_HOST=${KB_HOST:-${HOST#n8n.}}
  if [ -n "$HOST" ] && [ -n "$NET" ]; then
    NET_ARGS=(--network "$NET")
    LABELS=(-l traefik.enable=true
      -l "traefik.http.routers.kb.rule=Host(\`$KB_HOST\`) || (Host(\`$HOST\`) && PathPrefix(\`/kb\`))"
      -l traefik.http.routers.kb.priority=1000
      -l traefik.http.routers.kb.tls=true
      -l "traefik.http.routers.kb.entrypoints=${ENTRY:-websecure}"
      -l traefik.http.services.kb.loadbalancer.server.port=8765)
    [ -n "$RESOLVER" ] && LABELS+=(-l "traefik.http.routers.kb.tls.certresolver=$RESOLVER")
    PUBLIC_URL="https://$KB_HOST/kb"
  fi
fi
docker rm -f kb-api >/dev/null 2>&1 || true
docker run -d --name kb-api --restart unless-stopped \
  --user "$(id -u kb):$(id -g kb)" \
  -e KB_HOME=$KB -e KB_TOKEN="$TOKEN" -e PYTHONUNBUFFERED=1 -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$KB/kb:$KB/kb:ro" -v "$DATA:$DATA" \
  --memory 512m --read-only --tmpfs /tmp \
  "${NET_ARGS[@]}" "${LABELS[@]}" \
  python:3.12-slim python "$KB/kb/server.py" >/dev/null
echo "$PUBLIC_URL" > "$KB/.public_url"

echo "== 6/6 schedule (every 6 hours) and nightly backup"
cat > /etc/cron.d/kb <<EOF
SHELL=/bin/bash
15 */6 * * * kb cd $KB && KB_HOME=$KB $KB/venv/bin/python kb/ingest_all.py >> $DATA/logs/ingest.log 2>&1
40 3 * * * kb sqlite3 $DATA/kb.sqlite ".backup $DATA/backups/kb-\$(date +\%a).sqlite"
EOF
chmod 644 /etc/cron.d/kb
cat > /etc/logrotate.d/kb <<EOF
$DATA/logs/*.log { weekly rotate 8 compress missingok notifempty copytruncate }
EOF
systemctl enable --now cron >/dev/null 2>&1 || true

sleep 4
echo
echo "== checks"
if docker exec kb-api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8765/health').read().decode())"; then
  echo "API container: running"
else
  echo "API container: NOT answering"; docker logs --tail 30 kb-api
fi
if [[ "$PUBLIC_URL" == https://* ]]; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' "$PUBLIC_URL/health" || true)
  echo "Public HTTPS $PUBLIC_URL/health -> $CODE (200 means it works; a new certificate can take a minute)"
fi
echo
echo "Done. Search URL: $PUBLIC_URL"
TOKEN_SHOWN=$(grep '^KB_TOKEN=' "$ENVF" | cut -d= -f2)
if [[ "$PUBLIC_URL" == https://* ]]; then echo "Claude connector URL (keep private): $PUBLIC_URL/mcp/$TOKEN_SHOWN"; fi
echo "Your access token is in $ENVF (KB_TOKEN). Show it with:  grep KB_TOKEN $ENVF"
echo "Ingest log: $DATA/logs/ingest.log"
