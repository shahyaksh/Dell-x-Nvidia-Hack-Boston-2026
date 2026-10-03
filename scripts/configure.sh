#!/usr/bin/env bash
# Write config.sandbox.json for a real-Gmail run.
#   scripts/configure.sh <sender@gmail.com> <champion-inbox> <vp-inbox> [tracking-base-url]
# tracking-base-url defaults to the running cloudflared quick tunnel (tmux session outreach-tunnel).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SENDER="${1:?sender gmail}"; CHAMP="${2:?champion test inbox}"; VP="${3:?vp test inbox}"
URL="${4:-$(tmux capture-pane -pt outreach-tunnel -S -200 2>/dev/null | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | head -1)}"
[ -n "$URL" ] || { echo "no tunnel URL; start one: tmux new -d -s outreach-tunnel 'cloudflared tunnel --url http://127.0.0.1:8090'"; exit 1; }
[ -f "$HERE/.dashboard_password" ] || python3 -c "import secrets;print(secrets.token_urlsafe(12))" > "$HERE/.dashboard_password"
python3 - "$HERE/config.sandbox.json" "$SENDER" "$CHAMP" "$VP" "$URL" "$(cat "$HERE/.dashboard_password")" <<'PY'
import json, sys
path, sender, champ, vp, url, pw = sys.argv[1:]
json.dump({"mail_mode": "gmail", "sender_email": sender, "gmail_config": "/sandbox/hand/gmail_config.json",
           "track_base_url": url, "demo_contacts": {"champion": champ, "vp": vp}, "allowlist": [champ, vp],
           "dashboard_user": "admin", "dashboard_password": pw}, open(path, "w"), indent=2)
print(f"wrote {path}\n  sender={sender}\n  champion={champ}\n  vp={vp}\n  tracking={url}\n  dashboard login: admin / {pw}")
PY
chmod 600 "$HERE/config.sandbox.json"
