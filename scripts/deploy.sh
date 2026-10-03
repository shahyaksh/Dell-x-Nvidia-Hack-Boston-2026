#!/usr/bin/env bash
# Deploy the outreach harness into the NemoClaw/OpenShell sandbox and start dashboard + inbox watcher.
#   scripts/deploy.sh                 # upload code, (re)generate data if missing, install skills, start services
#   FRESH=1 scripts/deploy.sh         # also regenerate the dataset (wipes outreach state in the sandbox)
#   scripts/deploy.sh restart         # only restart dashboard/watcher
# Env: SANDBOX (default my-assistant), PORT (default 8090), GMAIL_CONFIG (host path to gmail_config.json)
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
source /home/dell/nemoclaw-offline/env.sh 2>/dev/null || true
SANDBOX="${SANDBOX:-my-assistant}"
PORT="${PORT:-8090}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/sandbox/AutomatedOutreach
say() { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
sx() { nemoclaw "$SANDBOX" exec --no-tty -- sh -c "$1" 2>&1 | { grep -v -E 'Active gateway|EHPA|trace-warnings' || true; }; }

start_services() {
  tmux kill-session -t outreach-dash 2>/dev/null || true
  tmux kill-session -t outreach-watch 2>/dev/null || true
  sx "pkill -f 'dashboard/server.py' ; pkill -f 'outreach.cli watch' ; true" >/dev/null || true
  tmux new -d -s outreach-dash "openshell sandbox exec -n $SANDBOX --no-tty -- sh -c 'cd $DEST && PORT=$PORT exec python3 -u dashboard/server.py'; echo exited; read _"
  mode=$(sx "cd $DEST && python3 -c 'from outreach import config; print(config.load()[\"mail_mode\"])'" | tail -1)
  if [ "$mode" = "gmail" ]; then
    tmux new -d -s outreach-watch "openshell sandbox exec -n $SANDBOX --no-tty -- sh -c 'cd $DEST && exec python3 -u -m outreach.cli watch --every 45'; echo exited; read _"
    say "inbox watcher started (tmux attach -t outreach-watch)"
  else
    say "mail_mode=$mode -> inbox watcher not started"
  fi
  openshell forward stop "$PORT" "$SANDBOX" >/dev/null 2>&1 || true
  openshell forward start "$PORT" "$SANDBOX" -d </dev/null >/tmp/outreach-forward.log 2>&1
  sleep 3
  curl -s --max-time 10 --retry 3 --retry-connrefused "http://127.0.0.1:$PORT/health" && echo && say "dashboard: http://127.0.0.1:$PORT  (tmux attach -t outreach-dash)"
}

if [ "${1:-}" = "restart" ]; then start_services; exit 0; fi

say "staging code"
STAGE="$(mktemp -d)/AutomatedOutreach"
mkdir -p "$STAGE"
tar -C "$HERE" --exclude='__pycache__' --exclude='data/outreach.db*' --exclude='data/outbox' --exclude='.git' -cf - . | tar -C "$STAGE" -xf -
[ -f "$HERE/config.sandbox.json" ] && cp "$HERE/config.sandbox.json" "$STAGE/config.json"
say "uploading to $SANDBOX:$DEST"
sx "mkdir -p $DEST" >/dev/null
nemoclaw "$SANDBOX" upload "$STAGE" /sandbox/ 2>&1 | tail -1

if [ -n "${GMAIL_CONFIG:-}" ]; then
  say "uploading Gmail app-password config"
  sx "mkdir -p /sandbox/hand && chmod 700 /sandbox/hand" >/dev/null
  nemoclaw "$SANDBOX" upload "$GMAIL_CONFIG" /sandbox/hand/gmail_config.json 2>&1 | tail -1
  sx "chmod 600 /sandbox/hand/gmail_config.json" >/dev/null
fi

if [ "${FRESH:-0}" = "1" ] || ! sx "test -f $DEST/data/outreach.db && echo yes" | grep -q yes; then
  say "generating 12-month dataset inside the sandbox"
  sx "cd $DEST && python3 -m outreach.cli init"
  sx "cd $DEST && python3 -m outreach.cli map-contacts" || true
fi

say "installing OpenClaw skills"
for s in outreach-analyst outreach-writer inbox-responder; do
  nemoclaw "$SANDBOX" skill install "$HERE/skills/$s" 2>&1 | { grep -v -E 'Active gateway|EHPA|trace-warnings' || true; } | tail -2
done
nemoclaw "$SANDBOX" upload "$HERE/openclaw/HEARTBEAT.md" /sandbox/.openclaw/workspace/HEARTBEAT.md 2>&1 | tail -1 || true

start_services
