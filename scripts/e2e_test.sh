#!/usr/bin/env bash
# End-to-end test against REAL Gmail, running inside the NemoClaw/OpenShell sandbox.
# Prereqs: config.sandbox.json (mail_mode=gmail, demo_contacts, track_base_url=<tunnel>), gmail preset applied,
#          scripts/deploy.sh done (dashboard on :8090 + watcher). Stages 5-7 need a human acting as the customer.
#   scripts/e2e_test.sh            # all stages
#   STAGES="0 1 2 3 4" scripts/e2e_test.sh
set -uo pipefail
export PATH="$HOME/.local/bin:$PATH"
source /home/dell/nemoclaw-offline/env.sh 2>/dev/null || true
SANDBOX="${SANDBOX:-my-assistant}"; PORT="${PORT:-8090}"; DEST=/sandbox/AutomatedOutreach
HERE="$(cd "$(dirname "$0")/.." && pwd)"
STAGES="${STAGES:-0 1 2 3 4 5 6 7 8}"
CFG="$HERE/config.sandbox.json"
PW=$(python3 -c "import json;print(json.load(open('$CFG'))['dashboard_password'])")
CHAMP=$(python3 -c "import json;print(json.load(open('$CFG'))['demo_contacts']['champion'])")
VP=$(python3 -c "import json;print(json.load(open('$CFG'))['demo_contacts']['vp'])")
CUST="$CHAMP"   # the address customer replies come FROM (VP may be a +alias of the same inbox)
SIM=""; [ -f "${CUSTOMER_CREDS:-$HOME/gmail_customer.json}" ] && SIM="python3 $HERE/scripts/customer_sim.py"
PASS=0; FAIL=0
ok()   { printf '\033[1;32m  PASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '\033[1;31m  FAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
hdr()  { printf '\n\033[1;36m== Stage %s\033[0m\n' "$*"; }
ask()  { printf '\033[1;33m  >>> %s\033[0m\n' "$*"; }
sx()   { nemoclaw "$SANDBOX" exec --no-tty -- sh -c "cd $DEST && $1" 2>&1 | { grep -v -E 'Active gateway|EHPA|trace-warnings' || true; }; }
stats(){ sx "python3 -m outreach.cli stats" > /tmp/outreach-stats.json; }
q()    { python3 -c "import json,sys;s=json.load(open('/tmp/outreach-stats.json'));print($1)"; }
api()  { curl -s --max-time 60 -u "admin:$PW" "$@"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }
wait_for() { # desc, python-expr over s, timeout seconds
  local t=0; while [ $t -lt "$3" ]; do stats; [ "$(q "$2")" = "True" ] && { ok "$1 (${t}s)"; return 0; }; sleep 15; t=$((t+15)); done
  bad "$1 (timed out after $3s)"; return 1; }
approve_all() { # approve every pending draft through the dashboard (the only send path)
  for id in $(api "http://127.0.0.1:$PORT/api/queue" | python3 -c "import json,sys;print(' '.join(str(r['id']) for r in json.load(sys.stdin)))"); do
    r=$(api -H 'Accept: application/json' -X POST -d "approver=e2e-human" "http://127.0.0.1:$PORT/queue/$id/approve")
    echo "    approve #$id -> $r"
  done; }

for S in $STAGES; do case $S in
0) hdr "0: sandbox + egress boundary"
   check "dashboard healthy, mail_mode=gmail" '[ "$(curl -s --max-time 10 http://127.0.0.1:$PORT/health | python3 -c "import json,sys;print(json.load(sys.stdin)[\"mail_mode\"])")" = gmail ]'
   out=$(sx "python3 -m outreach.cli egress-check"); echo "$out" | sed 's/^/    /'
   check "inference.local reachable"  'echo "$out" | grep -q "REACHABLE.*inference.local"'
   check "smtp.gmail.com reachable"   'echo "$out" | grep -q "REACHABLE.*smtp.gmail.com"'
   check "imap.gmail.com reachable"   'echo "$out" | grep -q "REACHABLE.*imap.gmail.com"'
   check "github.com blocked"         'echo "$out" | grep -q "BLOCKED.*github.com"'
   check "pypi.org blocked"           'echo "$out" | grep -q "BLOCKED.*pypi.org"' ;;
1) hdr "1: 12 months of usage data"
   stats
   check "365 days of usage"          '[ "$(q "s[\"usage\"][\"days\"]")" -ge 360 ]'
   check ">50k usage rows"            '[ "$(q "s[\"usage\"][\"rows_\"]")" -gt 50000 ]'
   check "demo account mapped to $CHAMP" '[ "$(q "bool(s[\"demo_account\"])")" = True ]' ;;
2) hdr "2: analyst agent ranks targets"
   out=$(sx "python3 -m outreach.cli analyze --segment expansion --top 5"); echo "$out" | sed 's/^/    /'
   DEMO=$(q "s['demo_account']['name']")
   check "demo account '$DEMO' is a top-5 expansion target" 'echo "$out" | grep -q "$DEMO"' ;;
3) hdr "3: writer agent drafts with local LLM (pending approval only)"
   stats; AID=$(q "s['demo_account']['id']")
   sx "python3 -m outreach.cli draft --segment expansion --account-id $AID --template champion_intro" | sed 's/^/    /'
   sx "python3 -m outreach.cli draft --segment expansion --account-id $AID --template vp_enterprise_pitch --audience vp" | sed 's/^/    /'
   sx "python3 -m outreach.cli draft --segment pro_upsell --top 2" | sed 's/^/    /'
   stats
   check "drafts are pending_approval, none sent" '[ "$(q "sum(r[\"n\"] for r in s[\"emails\"] if r[\"status\"]==\"pending_approval\")")" -ge 3 ]'
   check "no unresolved placeholders"  '[ "$(q "sum(1 for r in s[\"recent\"] if r[\"leftover\"] and r[\"status\"]==\"pending_approval\")")" = 0 ]'
   # negative: a draft to a non-allowlisted real-looking address must be blocked at send time
   sx "python3 -c \"from outreach import db;c=db.connect();c.execute(\\\"INSERT INTO outreach_emails(to_email,subject,body,status,tracking_token,created_at) VALUES ('ceo@realcompany.com','x','x','pending_approval','neg-test-1',datetime('now'))\\\");c.commit()\"" ;;
4) hdr "4: human approves in dashboard -> real Gmail send"
   approve_all; stats
   check "outbound emails sent via Gmail" '[ "$(q "sum(r[\"n\"] for r in s[\"emails\"] if r[\"status\"]==\"sent\" and r[\"kind\"]==\"outbound\")")" -ge 3 ]'
   check "non-allowlisted recipient blocked" '[ "$(q "any(r[\"to_email\"]==\"ceo@realcompany.com\" and r[\"status\"]==\"blocked\" for r in s[\"recent\"])")" = True ]'
   check "sent emails carry Message-IDs" '[ "$(q "all(r[\"message_id\"] for r in s[\"recent\"] if r[\"status\"]==\"sent\")")" = True ]'
   ask "Check $CHAMP and $VP inboxes: the champion-intro and VP-pitch emails should have arrived." ;;
5) hdr "5: tracking (open / click / convert)"
   if [ -n "$SIM" ]; then $SIM engage --to "$CHAMP" | sed 's/^/    /'
   else ask "In $CHAMP: OPEN the FlowDesk email (load images), CLICK the offer link, press 'Start upgrade'."; fi
   wait_for "click tracked"      "any(r['type']=='click' and r['n']>0 for r in s['events'])" 600
   wait_for "conversion tracked" "len(s['conversions'])>0" 600
   stats; check "open tracked" '[ "$(q "any(r[\"type\"]==\"open\" for r in s[\"events\"])")" = True ]' ;;
6) hdr "6: inbox agent -> classified reply -> threaded reply draft -> approve"
   if [ -n "$SIM" ]; then $SIM reply --to "$CHAMP" --cc "$VP" --body "Happy to help - I'm looping in our VP on this thread. Could we do a call Thursday afternoon?" | tail -1 | sed 's/^/    /'
   else ask "From $CHAMP, REPLY to the email and CC $VP with: 'Happy to help - looping in our VP. Could we do a call Thursday afternoon?'"; fi
   wait_for "reply ingested + matched" "any(i['matched_email_id'] and i['from_email']=='${CUST,,}' for i in s['inbound'])" 900
   stats; echo "    intents: $(q "[(i['from_email'], i['intent']) for i in s['inbound']]")"
   check "intent is intro_to_vp / meeting_request / interested" '[ "$(q "any(i[\"intent\"] in (\"intro_to_vp\",\"meeting_request\",\"interested\") for i in s[\"inbound\"])")" = True ]'
   wait_for "reply draft waiting for approval" "any(r['kind']=='reply' and r['status']=='pending_approval' for r in s['recent'])" 180
   approve_all; stats
   check "reply sent in-thread (In-Reply-To set)" '[ "$(q "any(r[\"kind\"]==\"reply\" and r[\"status\"]==\"sent\" and r[\"in_reply_to\"] for r in s[\"recent\"])")" = True ]'
   if [ -n "$SIM" ]; then $SIM find --to "$CHAMP" --subject-has "Re:" --wait 180 | grep -E '"subject"|"cc"|folder' | sed 's/^/    customer sees: /'; fi ;;
7) hdr "7: unsubscribe -> suppression -> blocked"
   VPCREDS="${VP_CREDS:-$HOME/gmail_vp.json}"; UNSUB="$CUST"
   if [ -n "$SIM" ] && [ -f "$VPCREDS" ]; then UNSUB="$VP"; CUSTOMER_CREDS="$VPCREDS" $SIM reply --to "$VP" --body "Please unsubscribe me." | tail -1 | sed 's/^/    /'
   elif [ -n "$SIM" ]; then $SIM reply --to "$VP" --body "Please unsubscribe me." | tail -1 | sed 's/^/    /'
   else ask "From $VP, reply to the VP email with just: 'Please unsubscribe me.'"; UNSUB="$VP"; fi
   wait_for "$UNSUB suppressed" "'${UNSUB,,}' in s['suppression']" 600
   stats; AID=$(q "s['demo_account']['id']")
   if [ "$UNSUB" = "$VP" ]; then TPL="--template vp_enterprise_pitch --audience vp"; else TPL="--template champion_intro"; fi
   sx "python3 -m outreach.cli draft --segment expansion --account-id $AID $TPL" | sed 's/^/    /'
   approve_all; stats
   check "new send to suppressed $UNSUB blocked" '[ "$(q "any(r[\"to_email\"].lower()==\"${UNSUB,,}\" and r[\"status\"]==\"blocked\" and \"suppressed\" in (r[\"status_detail\"] or \"\") for r in s[\"recent\"])")" = True ]' ;;
8) hdr "8: OpenClaw agent summarizes via skills"
   out=$(timeout 600 nemoclaw "$SANDBOX" agent --agent main --session-id "e2e-$(date +%s)" -m "Use the outreach-analyst skill to run the report command and summarize live campaign results: sent, clicks, replies, conversions and MRR uplift." 2>&1 | grep -v -E 'Active gateway|EHPA|trace-warnings|\[gateway\]')
   echo "$out" | tail -25 | sed 's/^/    /'
   check "agent answered with campaign numbers" 'echo "$out" | grep -qiE "sent|conver"' ;;
esac; done
printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"; [ "$FAIL" = 0 ]
