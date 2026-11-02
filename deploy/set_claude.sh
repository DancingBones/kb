#!/usr/bin/env bash
# Sets up AI summaries on your Claude plan (not the API): installs Claude Code for the kb user,
# saves a long-lived sign-in token, tests it, and turns on summaries.
set -euo pipefail
KB=/opt/kb; ENVF=$KB/.env
[ "$(id -u)" = 0 ] || { echo "Run as root"; exit 1; }

# The kb user gets its own writable home folder for Claude Code (the code folder stays root-owned).
KBH=/home/kb
mkdir -p "$KBH"; chown kb:kb "$KBH"; chmod 700 "$KBH"
CL=$KBH/.local/bin/claude
if [ ! -x "$CL" ]; then
  echo "== Installing Claude Code for the kb user"
  sudo -u kb env HOME="$KBH" bash -c 'cd ~ && curl -fsSL https://claude.ai/install.sh | bash' >/dev/null
fi
"$CL" --version

cat <<'TXT'

== Sign in with your Claude plan
Next, Claude Code prints a link. Open it, sign in to claude.ai, approve, and paste the code it shows
back here. At the end it prints a long token starting with sk-ant-oat. Copy that whole token.
TXT
read -r -p "Press Enter to start..." _
sudo -u kb env HOME="$KBH" "$CL" setup-token || true

printf '\033[?2004l'
echo
read -r -s -p "Paste the sk-ant-oat token (right-click, stays hidden), then Enter: " T; echo
T=$(printf '%s' "$T" | sed 's/\x1b\[[0-9]*~//g' | tr -d '[:space:]~')
# A paste that landed twice: keep one copy
L=${#T}; if [ $((L % 2)) -eq 0 ] && [ "${T:0:$((L/2))}" = "${T:$((L/2))}" ]; then T=${T:0:$((L/2))}; fi
[[ "$T" == sk-ant-oat* ]] || { echo "That doesn't look like the token (should start with sk-ant-oat). Run this again."; exit 1; }

sed -i '/^CLAUDE_CODE_OAUTH_TOKEN=/d;/^KB_AUTO_SUMMARIZE=/d' "$ENVF"
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\nKB_AUTO_SUMMARIZE=1\n' "$T" >> "$ENVF"
chown kb:kb "$ENVF"; chmod 600 "$ENVF"

echo "== Testing"
if OUT=$(cd /tmp && echo "Reply with just: ok" | sudo -u kb env HOME="$KBH" CLAUDE_CODE_OAUTH_TOKEN="$T" "$CL" -p --model haiku --tools "" 2>&1); then
  echo "Claude replied: $OUT"
else
  echo "Test failed: $OUT"; exit 1
fi
sudo -u kb KB_HOME=$KB "$KB/venv/bin/python" "$KB/kb/summarize.py" --estimate
echo
echo "Summaries are ON. Up to 150 items are summarized on each 6-hour run, using your plan's Haiku usage."
echo "To start a batch right now:  sudo -u kb KB_HOME=$KB $KB/venv/bin/python $KB/kb/summarize.py --limit 50"
echo "To turn them off: set KB_AUTO_SUMMARIZE=0 in $ENVF"
