#!/usr/bin/env python3
"""Human-in-the-loop dashboard + tracking server (stdlib only; runs inside the OpenShell sandbox).

  python3 dashboard/server.py            -> http://127.0.0.1:8090  (host: openshell forward start 8090 my-assistant -d)

Operator pages (HTTP Basic auth):  /  /queue  /sent  /inbox  /targets  /api/report  /api/queue
Public tracking (reachable through the tunnel): /t/o/<tok>.gif  /t/c/<tok>  /offer/<tok>  /t/convert/<tok>  /u/<tok>
Approve & Send is the only path that calls mailer.send().
"""
import base64
import hashlib
import hmac
import html
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from outreach import agent, cli, config, db, mailer, scoring, traces  # noqa: E402

CFG = config.load()
SESSION_TOKEN = hmac.new(CFG["dashboard_password"].encode(), b"fd-session-v1", hashlib.sha256).hexdigest()
PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
UPGRADE_MRR = {"enterprise_trial": lambda a: a["seats_purchased"] * 25, "pro_20": lambda a: a["seats_purchased"] * 12,
               "winback_2mo": lambda a: a["mrr"] * 0.5, "reactivation_guide": lambda a: a["seats_purchased"] * 15}
TARGET_PLAN = {"enterprise_trial": "enterprise", "pro_20": "pro"}

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--mut:#5d6672;--line:#e3e6ea;--acc:#76b900;--acc2:#0b6bcb;--warn:#c4410c;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a20;--fg:#e8eaed;--mut:#9aa3ad;--line:#2a2f37;--acc2:#58a6ff;--warn:#ff7b54;--ok:#56d364}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:18px;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--card);flex-wrap:wrap}
header b{font-size:16px}header b span{color:var(--acc)}nav a{color:var(--mut);text-decoration:none;margin-right:14px;font-weight:500}nav a:hover{color:var(--fg)}
main{max-width:1200px;margin:0 auto;padding:20px 16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.k{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}.v{font-size:26px;font-weight:650;margin-top:2px}
table{width:100%;border-collapse:collapse}td,th{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mut);font-weight:600;font-size:12px}
.bar{height:10px;background:var(--acc);border-radius:3px}.bar.b2{background:var(--acc2)}.pill{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid var(--line);font-size:12px;color:var(--mut)}
.sent{color:var(--ok)}.blocked,.failed,.rejected{color:var(--warn)}textarea,input[type=text]{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:8px;font:13px/1.45 ui-monospace,Menlo,monospace}
textarea{min-height:300px}button{border:0;border-radius:6px;padding:8px 14px;font-weight:600;cursor:pointer}.approve{background:var(--acc);color:#111}.reject{background:transparent;color:var(--warn);border:1px solid var(--warn)}
.mut{color:var(--mut)}h2{margin:6px 0 12px;font-size:18px}h3{margin:0 0 8px;font-size:15px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.why{background:var(--bg);border-left:3px solid var(--acc2);padding:6px 10px;margin:8px 0;color:var(--mut)}
.badge{display:inline-block;min-width:18px;padding:0 6px;margin-left:4px;border-radius:99px;background:var(--warn);color:#fff;font-size:11px;font-weight:700;text-align:center}
nav a.on{color:var(--fg);border-bottom:2px solid var(--acc)}.small{font-size:12px}.sev-high{border-left:4px solid var(--warn)}.sev-medium{border-left:4px solid #e8a10a}.sev-low{border-left:4px solid var(--line)}
.st.ok{color:var(--ok)}.st.error{color:var(--warn)}.st.running{color:var(--acc2)}
details.step{border:1px solid var(--line);border-radius:8px;margin:6px 0;background:var(--card)}details.step summary{cursor:pointer;padding:6px 8px;display:flex;gap:6px;align-items:center;list-style:none}
details.step.error{border-color:var(--warn)}details.step .ms{margin-left:auto;color:var(--mut);font-size:11px}details.step .agent{color:var(--mut);font-size:11px}
details.step pre,.artifact pre{margin:0;padding:8px;max-height:260px;overflow:auto;background:var(--bg);font:12px/1.4 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-word}
.lbl{font-size:11px;color:var(--mut);padding:4px 8px 0}.card pre{white-space:pre-wrap;word-break:break-word;font:12px/1.45 ui-monospace,Menlo,monospace}
/* chat console */
body.app{height:100vh;display:flex;flex-direction:column;overflow:hidden}body.app header{flex:none}
.console{display:grid;grid-template-columns:230px minmax(0,1fr) 400px;flex:1;min-height:0}.console>*{min-height:0}
.side{border-right:1px solid var(--line);padding:14px;overflow:auto;background:var(--card)}.side h3{font-size:12px;color:var(--mut);text-transform:uppercase;margin:14px 0 6px}
.side a{display:block;padding:6px 8px;border-radius:6px;color:var(--fg);text-decoration:none;font-size:13px}.side a.on,.side a:hover{background:var(--bg)}
.chip{display:block;width:100%;text-align:left;margin:4px 0;padding:7px 9px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:12px/1.35 system-ui;font-weight:500}
.chip:hover{border-color:var(--acc)}
.center{display:flex;flex-direction:column;min-width:0;min-height:0}#msgs{flex:1;overflow:auto;padding:18px 22px}
.msg{max-width:900px;margin:0 auto 16px}.msg .who{font-size:12px;color:var(--mut);margin-bottom:4px}.msg.user .body{background:var(--acc2);color:#fff;display:inline-block;padding:9px 13px;border-radius:12px}
.msg.assistant .body{background:var(--card);border:1px solid var(--line);padding:10px 14px;border-radius:12px}.msg .body p{margin:4px 0}.msg .body ul{margin:4px 0;padding-left:20px}
.tbl{overflow:auto}.tbl table{font-size:12.5px;margin:6px 0}.artifact{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;background:var(--bg)}
.a-title{font-weight:650;margin-bottom:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}.chartbox{position:relative;height:280px}.explain{margin-top:8px}
.artifact details summary{cursor:pointer;color:var(--mut);font-size:12px;margin-top:6px}.draft{padding:6px 0;border-top:1px solid var(--line)}
.btn{margin-left:auto;font-size:12px;font-weight:600;color:var(--acc2);text-decoration:none}.tracelink{font-size:12px;color:var(--acc2)}
.artifact.esc-high{border-left:4px solid var(--warn)}.artifact.esc-medium{border-left:4px solid #e8a10a}
form#form{display:flex;gap:8px;padding:12px 18px;border-top:1px solid var(--line);background:var(--card)}
#q{flex:1;padding:11px 12px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font:14px system-ui}
#send{background:var(--acc);color:#111}select{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px}
.tracep{border-left:1px solid var(--line);display:flex;flex-direction:column;min-width:0;background:var(--bg)}#trace-head{padding:10px 12px;border-bottom:1px solid var(--line);font-size:13px}
#trace{flex:1;overflow:auto;padding:6px 10px}.thinking{color:var(--mut);font-style:italic}.dots::after{content:"...";animation:d 1.2s steps(4) infinite}
@keyframes d{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}}
@media (max-width:1100px){body.app{height:auto;overflow:auto}.console{grid-template-columns:1fr;height:auto}.side{display:none}.tracep{display:none;max-height:60vh}
 body.show-trace .tracep{display:flex}#msgs{min-height:60vh}}
"""


def nav_counts():
    try:
        with db.connect(CFG) as c:
            q = c.execute("SELECT COUNT(*) FROM outreach_emails WHERE status='pending_approval'").fetchone()[0]
            e = c.execute("SELECT COUNT(*) FROM escalations WHERE status='open'").fetchone()[0]
        return q, e
    except Exception:
        return 0, 0


def page(title, body, refresh=None, active="", wide=False, scripts=""):
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    q, e = nav_counts()
    badge = lambda n: f"<span class='badge'>{n}</span>" if n else ""
    links = [("/", "Agent chat", ""), ("/overview", "Overview", ""), ("/targets", "Targets", ""),
             ("/queue", "Approval queue", badge(q)), ("/escalations", "Escalations", badge(e)), ("/traces", "Agent traces", ""),
             ("/sent", "Sent &amp; tracking", ""), ("/inbox", "Inbox", "")]
    navh = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}{b}</a>' for h, t, b in links)
    main = body if wide else f"<main>{body}</main>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>{meta}<style>{CSS}</style></head><body class="{'app' if wide else ''}">
<header><b>Flow<span>Desk</span> Revenue Agents</b><nav>{navh}</nav>
<span class="mut small" style="margin-left:auto">Nemotron on Dell GB10 &middot; OpenShell sandbox &middot; mail: {CFG['mail_mode']} &middot; <a href="/logout">sign out</a></span></header>
{main}{scripts}</body></html>"""


SUGGESTIONS = [
    "How many active users do we have in each EMEA country over the last 30 days? Plot it.",
    "Show weekly active users over the last 12 months by region.",
    "Which paywall features do Team-plan accounts hit most?",
    "What share of MRR comes from each plan?",
    "Who are the top 3 Enterprise expansion targets in APAC? Draft champion intro emails for the top 2.",
    "Which free-plan power users should we upsell to Pro?",
    "Check the inbox for replies and tell me what needs my attention.",
    "How are live agent campaigns performing vs last year's blasts?",
    "Flag Lattice Capital for the VP of Sales: they asked for a custom security review before upgrading.",
]


def login_page(next_url="/", error=""):
    err = f"<div class='blocked' style='margin-bottom:10px'>{esc(error)}</div>" if error else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in</title><style>{CSS}</style></head><body>
<main style="max-width:380px;margin-top:12vh"><div class="card"><h2>Flow<span style="color:var(--acc)">Desk</span> Revenue Agents</h2>
<p class="mut small">Local Nemotron agents in an NVIDIA OpenShell sandbox</p>{err}
<form method="post" action="/login"><input type="hidden" name="next" value="{esc(next_url)}">
<label class="small mut">Username</label><input type="text" name="user" value="{esc(CFG['dashboard_user'])}" autocomplete="username">
<label class="small mut" style="display:block;margin-top:10px">Password</label>
<input type="password" name="password" autofocus autocomplete="current-password" style="width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)">
<button class="approve" style="width:100%;margin-top:14px">Sign in</button></form></div></main></body></html>"""


def chat_page():
    chips = "".join(f'<button class="chip">{esc(t)}</button>' for t in SUGGESTIONS)
    body = f"""<div class="console">
<aside class="side"><a href="#" id="new">+ New conversation</a>
<h3>Agent</h3><select id="mode" style="width:100%"><option value="harness">Revenue agent (tools + traces)</option>
<option value="openclaw">OpenClaw main agent (NemoClaw skills)</option></select>
<h3>Try asking</h3>{chips}<h3>Conversations</h3><div id="sessions"></div></aside>
<section class="center"><div id="msgs"></div>
<form id="form"><input id="q" autocomplete="off" placeholder="Ask about usage, targets, outreach, replies..."><button id="send">Send</button>
<a href="#" id="toggle-trace" class="small" style="align-self:center">trace</a></form></section>
<aside class="tracep"><div id="trace-head" class="mut">Agent trace: every LLM call, tool call, guard and escalation appears here live.</div>
<div id="trace"></div></aside></div>"""
    return page("Agent chat", body, active="/", wide=True,
                scripts='<script src="/static/chart.umd.min.js"></script><script src="/static/app.js"></script>')


def run_detail(conn, rid):
    run = db.one(conn, "SELECT * FROM agent_runs WHERE id=?", (rid,))
    if not run:
        return None
    steps = db.rows(conn, """SELECT s.*, r.agent FROM agent_steps s JOIN agent_runs r ON r.id=s.run_id
                             WHERE r.id=? OR r.parent_id=? ORDER BY s.ts, s.id""", (rid, rid))
    msg = db.one(conn, "SELECT content, artifacts FROM chat_messages WHERE run_id=? AND role='assistant'", (rid,))
    return {"run": run, "steps": steps, "message": msg}


def traces_page(conn, agent_filter=None):
    where, args = ("WHERE r.parent_id IS NULL" + (" AND r.agent=?" if agent_filter else "")), ([agent_filter] if agent_filter else [])
    runs = db.rows(conn, f"""SELECT r.*, (SELECT COUNT(*) FROM agent_steps s JOIN agent_runs c ON c.id=s.run_id
            WHERE c.id=r.id OR c.parent_id=r.id) n_steps,
            (SELECT COUNT(*) FROM agent_steps s JOIN agent_runs c ON c.id=s.run_id
            WHERE (c.id=r.id OR c.parent_id=r.id) AND s.kind='guard') n_guards
            FROM agent_runs r {where} ORDER BY r.id DESC LIMIT 200""", args)
    agents = [r["agent"] for r in db.rows(conn, "SELECT DISTINCT agent FROM agent_runs WHERE parent_id IS NULL")]
    filt = " ".join(f'<a class="pill" href="/traces?agent={esc(a)}">{esc(a)}</a>' for a in agents) + ' <a class="pill" href="/traces">all</a>'
    trs = "".join(f"<tr><td><a href='/traces/{r['id']}'>#{r['id']}</a></td><td>{esc(r['agent'])}</td>"
                  f"<td>{esc((r['input'] or '')[:140])}</td><td class='st {r['status']}'>{r['status']}</td><td>{r['n_steps']}</td>"
                  f"<td>{r['n_guards'] or ''}</td><td>{(r['ms'] or 0) / 1000:.1f}s</td><td class='mut'>{esc(r['started_at'])}</td></tr>"
                  for r in runs)
    return page("Agent traces", f"<h2>Agent traces</h2><div class='card'>{filt}</div><div class='card'><table><tr><th>Run</th>"
                f"<th>Agent</th><th>Input</th><th>Status</th><th>Steps</th><th>Guards</th><th>Time</th><th>Started</th></tr>{trs}"
                f"</table></div>", active="/traces", refresh=20)


def trace_page(conn, rid):
    d = run_detail(conn, rid)
    if not d:
        return None
    icon = {"llm": "&#129504;", "tool": "&#128295;", "guard": "&#128737;", "escalation": "&#9873;", "info": "&#8505;", "error": "&#9888;"}

    def pre(x):
        try:
            x = json.dumps(json.loads(x), indent=2)
        except (TypeError, ValueError):
            pass
        return f"<pre>{esc(x)}</pre>"
    steps = "".join(f"<details class='step {s['kind']} {s['status']}'><summary><span>{icon.get(s['kind'], '&bull;')}</span>"
                    f"<span class='agent'>{esc(s['agent'])}</span> <b>{esc(s['name'])}</b><span class='ms'>"
                    f"{(str(s['ms']) + 'ms') if s['ms'] is not None else ''} &middot; {esc(s['ts'][11:])}</span></summary>"
                    + (f"<div class='lbl'>input</div>{pre(s['input'])}" if s['input'] else "")
                    + (f"<div class='lbl'>output</div>{pre(s['output'])}" if s['output'] else "") + "</details>"
                    for s in d["steps"])
    r = d["run"]
    head = (f"<h2>{esc(r['agent'])} run #{r['id']} <span class='st {r['status']}'>{r['status']}</span></h2>"
            f"<div class='card'><div class='k'>Input</div>{pre(r['input'])}<div class='k'>Output</div>{pre(r['output'])}"
            f"<div class='mut small'>model {esc(r['model'])} &middot; {(r['ms'] or 0) / 1000:.1f}s &middot; {len(d['steps'])} steps"
            f" &middot; session {esc(r['session_id'])}</div></div>")
    return page(f"Trace #{rid}", head + f"<div class='card'><h3>Steps</h3>{steps}</div>", active="/traces")


def escalations_page(conn, flash=""):
    traces.resolve_approval_escalations(conn)
    rows = db.rows(conn, """SELECT e.*, a.name account FROM escalations e LEFT JOIN accounts a ON a.id=e.account_id
                            ORDER BY e.status='resolved', CASE e.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                            e.id DESC LIMIT 200""")
    cards = ""
    for e in rows:
        try:
            det = json.loads(e["detail"]) if e["detail"] and e["detail"].strip().startswith("{") else e["detail"]
        except ValueError:
            det = e["detail"]
        det_html = "".join(f"<div><span class='mut'>{esc(k)}:</span> {esc(v)}</div>" for k, v in det.items()) \
            if isinstance(det, dict) else esc(det or "")
        links = []
        if e["email_id"]:
            st = db.one(conn, "SELECT status FROM outreach_emails WHERE id=?", (e["email_id"],))
            links.append(f"<a href='/queue'>email #{e['email_id']} ({esc(st['status'] if st else '?')})</a>")
        if e["kind"] == "approval_needed" and e["status"] == "open":
            links.insert(0, "<a href='/queue'><b>Review drafts in Approval queue &rarr;</b></a>")
        if e["run_id"]:
            links.append(f"<a href='/traces/{e['run_id']}'>agent trace #{e['run_id']}</a>")
        action = (f"<form method='post' action='/escalations/{e['id']}/resolve' class='row' style='margin-top:8px'>"
                  f"<input type='text' name='note' placeholder='resolution note (optional)' style='flex:1'>"
                  f"<input type='text' name='who' value='{esc(CFG['dashboard_user'])}' style='width:120px'>"
                  f"<button class='approve'>Resolve</button></form>") if e["status"] == "open" else \
            f"<div class='mut small'>resolved by {esc(e['resolved_by'])} at {esc(e['resolved_at'])}</div>"
        cards += (f"<div class='card sev-{e['severity']}' style='{'opacity:.6' if e['status'] != 'open' else ''}'>"
                  f"<div class='row'><h3 style='margin:0'>{esc(e['title'])}</h3><span class='pill'>{esc(e['kind'])}</span>"
                  f"<span class='pill'>{esc(e['severity'])}</span><span class='pill'>from {esc(e['source'])}</span>"
                  f"<span class='mut small' style='margin-left:auto'>{esc(e['created_at'])}</span></div>"
                  f"<div style='margin:6px 0'>{det_html}</div><div class='row small'>"
                  f"{('<span>' + esc(e['account']) + '</span>') if e['account'] else ''}{' &middot; '.join(links)}</div>{action}</div>")
    n_open = sum(1 for e in rows if e["status"] == "open")
    return page("Escalations", f"<h2>Escalations ({n_open} open)</h2><p class='mut'>Everything the agents could not or "
                f"should not decide on their own: hot leads, pricing and security questions, drafts waiting for approval, "
                f"blocked sends, guard fallbacks and won deals.</p>{flash}{cards or '<div class=card>Nothing escalated.</div>'}",
                active="/escalations", refresh=30)


def esc(x):
    return html.escape("" if x is None else str(x))


def funnel_rows(g, color=""):
    s = g["sent"] or 1
    out = ""
    for k in ("sent", "opened", "clicked", "replied", "converted"):
        pct = 100 * g[k] / s
        out += (f"<tr><td style='width:90px'>{k}</td><td style='width:60px'>{g[k]}</td><td>"
                f"<div class='bar {color}' style='width:{max(pct, 0.5):.1f}%'></div></td><td style='width:60px'>{pct:.0f}%</td></tr>")
    return out


def overview(conn):
    r = cli.report(conn)
    hist = r["by_source"].get("historical", dict(sent=0, opened=0, clicked=0, replied=0, converted=0))
    live = r["by_source"].get("live", dict(sent=0, opened=0, clicked=0, replied=0, converted=0))
    pend = conn.execute("SELECT COUNT(*) FROM outreach_emails WHERE status='pending_approval'").fetchone()[0]
    usage = db.one(conn, "SELECT COUNT(*) rows_, COUNT(DISTINCT user_id) users, MIN(date) d0, MAX(date) d1 FROM usage_daily")
    mrr = {m["source"]: m["mrr"] for m in r["mrr_by_source"]}
    segs = {}
    for a in scoring.account_stats(conn):
        segs[a["segment"]] = segs.get(a["segment"], 0) + 1
    tiles = "".join(f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in [
        ("Usage rows (12 mo)", f"{usage['rows_']:,}"), ("Active users", usage["users"]),
        ("Awaiting approval", f"<a href='/queue'>{pend}</a>"), ("Live emails sent", live["sent"]),
        ("Live replies", live["replied"]), ("Live MRR uplift", f"${mrr.get('live', 0) or 0:,.0f}")])
    camp = "".join(f"<tr><td>{esc(c['name'])}</td><td><span class='pill'>{esc(c['segment'])}</span></td><td>{esc(c['source'])}</td>"
                   f"<td>{c['sent']}</td><td>{c['opened']}</td><td>{c['clicked']}</td><td>{c['replied']}</td><td>{c['converted']}</td>"
                   f"<td>{c['pending']}</td></tr>" for c in r["campaigns"][:20])
    seg = " ".join(f"<span class='pill'>{esc(k)}: {v}</span>" for k, v in sorted(segs.items()))
    body = f"""<div class="grid">{tiles}</div>
<div class="card"><h3>Account segments (from {usage['d0']} to {usage['d1']})</h3>{seg}</div>
<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))">
<div class="card"><h3>Baseline: last year's generic blasts</h3><table>{funnel_rows(hist, 'b2')}</table></div>
<div class="card"><h3>Live: local-LLM personalized + human approved</h3><table>{funnel_rows(live)}</table></div></div>
<div class="card"><h3>Campaigns</h3><table><tr><th>Campaign</th><th>Segment</th><th>Source</th><th>Sent</th><th>Opened</th>
<th>Clicked</th><th>Replied</th><th>Converted</th><th>Pending</th></tr>{camp}</table></div>"""
    return page("Outreach overview", body, refresh=20, active="/overview")


def targets_page(conn):
    out = ""
    for seg in ("expansion", "pro_upsell", "winback", "reactivation"):
        rows = "".join(
            f"<tr><td>{a['id']}</td><td>{esc(a['name'])}</td><td>{a['plan']}</td><td>{a['score']}</td>"
            f"<td>{a['active_users_30']}/{a['seats_purchased']} ({a['seat_util']}%)</td><td>{a['power_users']}</td>"
            f"<td>{round(a['growth'] * 100):+d}%</td><td>{a['paywall_90']}</td><td>{esc(', '.join(a['top_locked']))}</td>"
            f"<td>{esc((a['champion'] or {}).get('name'))}<br><span class='mut'>{esc((a['champion'] or {}).get('email'))}</span></td>"
            f"<td>{esc(a['vp_name'])}<br><span class='mut'>{esc(a['vp_email'])}</span></td></tr>"
            for a in scoring.targets(conn, seg, 8))
        out += (f"<div class='card'><h3>{seg}</h3><table><tr><th>ID</th><th>Account</th><th>Plan</th><th>Score</th>"
                f"<th>Active/seats</th><th>Power users</th><th>QoQ</th><th>Paywall hits</th><th>Locked features</th>"
                f"<th>Champion</th><th>VP</th></tr>{rows}</table></div>")
    return page("Targets", active="/targets", body="<h2>Agent-ranked targets (deterministic scoring over 12 months of usage)</h2>" + out)


def queue_page(conn, flash=""):
    rows = db.rows(conn, "SELECT e.*, a.name account FROM outreach_emails e LEFT JOIN accounts a ON a.id=e.account_id "
                         "WHERE e.status='pending_approval' ORDER BY e.kind DESC, e.id")
    cards = ""
    for e in rows:
        inbound = db.one(conn, "SELECT * FROM inbound_messages WHERE id=?", (e["inbound_id"],)) if e["inbound_id"] else None
        thread = (f"<div class='why'><b>Customer wrote</b> ({esc(inbound['intent'])}, {esc(inbound['sentiment'])}):<br>"
                  f"{esc(inbound['body'][:1200])}</div>") if inbound else ""
        allowed = all(mailer.is_allowed(x.strip(), CFG) for x in [e["to_email"]] + (e["cc_emails"] or "").split(",") if x.strip())
        warn = "" if allowed else "<span class='blocked'> &#9888; recipient not on allowlist - send will be blocked</span>"
        cards += f"""<div class="card"><div class="row"><h3 style="margin:0">#{e['id']} &middot; {esc(e['kind'])} &middot; {esc(e['account'])}</h3>
<span class="pill">{esc(e['template_id'] or 'reply')}</span><span class="pill">{esc(e['model'])} &middot; {e['llm_seconds'] or 0}s</span></div>
<div class="mut">To: {esc(e['to_email'])}{(' &middot; Cc: ' + esc(e['cc_emails'])) if e['cc_emails'] else ''}{warn}</div>
<div class="why"><b>Agent rationale:</b> {esc(e['rationale'])}</div>{thread}
<form method="post" action="/queue/{e['id']}/approve"><input type="text" name="subject" value="{esc(e['subject'])}">
<textarea name="body">{esc(e['body'])}</textarea><div class="row" style="margin-top:8px">
<input type="text" name="approver" value="{esc(CFG['dashboard_user'])}" style="width:160px">
<button class="approve">Approve &amp; Send</button>
<button class="reject" formaction="/queue/{e['id']}/reject">Reject</button></div></form></div>"""
    if not rows:
        cards = "<div class='card mut'>Nothing waiting. Ask the agent to draft a campaign, or wait for the inbox agent.</div>"
    return page("Approval queue", f"<h2>Approval queue ({len(rows)})</h2>{flash}{cards}", refresh=None, active="/queue")


def sent_page(conn):
    rows = db.rows(conn, """SELECT e.id, e.kind, e.to_email, e.cc_emails, e.subject, e.status, e.status_detail, e.sent_at,
        e.approved_by, a.name account,
        (SELECT GROUP_CONCAT(type || '@' || substr(ts,12,5), ', ') FROM email_events v WHERE v.email_id=e.id) events
        FROM outreach_emails e LEFT JOIN accounts a ON a.id=e.account_id
        WHERE e.status IN ('sent','blocked','failed','rejected','approved') AND (e.model IS NULL OR e.model!='generic-template')
        ORDER BY e.id DESC LIMIT 100""")
    trs = "".join(f"<tr><td>#{r['id']}</td><td>{esc(r['kind'])}</td><td>{esc(r['account'])}</td><td>{esc(r['to_email'])}</td>"
                  f"<td>{esc(r['subject'])}</td><td class='{r['status']}'>{r['status']}<br><span class='mut'>{esc(r['status_detail'])}</span></td>"
                  f"<td>{esc(r['approved_by'])}</td><td>{esc(r['sent_at'])}</td><td>{esc(r['events'])}</td></tr>" for r in rows)
    return page("Sent", f"<h2>Sent &amp; tracking</h2><div class='card'><table><tr><th>ID</th><th>Kind</th><th>Account</th>"
                        f"<th>To</th><th>Subject</th><th>Status</th><th>Approved by</th><th>Sent</th><th>Events</th></tr>{trs}"
                        f"</table></div>", refresh=15, active="/sent")


def inbox_page(conn):
    rows = db.rows(conn, "SELECT * FROM inbound_messages ORDER BY id DESC LIMIT 100")
    trs = "".join(f"<tr><td>{esc(r['received_at'])}</td><td>{esc(r['from_name'])}<br><span class='mut'>{esc(r['from_email'])}</span></td>"
                  f"<td>{esc(r['subject'])}<div class='mut'>{esc((r['body'] or '')[:300])}</div></td>"
                  f"<td><span class='pill'>{esc(r['intent'])}</span><br>{esc(r['sentiment'])}</td><td>{esc(r['summary'])}</td>"
                  f"<td>{('#' + str(r['matched_email_id'])) if r['matched_email_id'] else '-'}</td></tr>" for r in rows)
    sup = db.rows(conn, "SELECT * FROM suppression")
    return page("Inbox", f"<h2>Inbox agent</h2><div class='card'><table><tr><th>Received</th><th>From</th><th>Message</th>"
                         f"<th>Intent</th><th>Summary</th><th>Matched</th></tr>{trs}</table></div>"
                         f"<div class='card'><h3>Suppression list</h3>{'<br>'.join(esc(s['email'] + ' - ' + (s['reason'] or '')) for s in sup) or '<span class=mut>empty</span>'}</div>",
                refresh=15, active="/inbox")


def offer_page(conn, e, done=False):
    offer = db.one(conn, "SELECT * FROM offers WHERE id=?", (e["offer_id"],)) or {"headline": "FlowDesk", "details": ""}
    acct = db.one(conn, "SELECT * FROM accounts WHERE id=?", (e["account_id"],)) or {}
    if done:
        inner = f"<h2>You're all set &#127881;</h2><p>{esc(acct.get('name'))} is now on the upgraded plan. Your success manager will reach out shortly.</p>"
    else:
        inner = (f"<h2>{esc(offer['headline'])}</h2><p>{esc(offer['details'])}</p>"
                 f"<p class='mut'>Prepared for {esc(acct.get('name'))}</p>"
                 f"<form method='post' action='/t/convert/{esc(e['tracking_token'])}'><button class='approve'>Start upgrade</button></form>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlowDesk offer</title><style>{CSS}</style></head><body><main style="max-width:640px"><div class="card">{inner}</div></main></body></html>"""


class H(BaseHTTPRequestHandler):
    server_version = "OutreachDashboard/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to):
        self._send(303, "", headers={"Location": to})

    def _authed(self):
        want = base64.b64encode(f"{CFG['dashboard_user']}:{CFG['dashboard_password']}".encode()).decode()
        if self.headers.get("Authorization") == f"Basic {want}":      # scripts / curl
            return True
        cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
        if hmac.compare_digest(cookies.get("fd_session", ""), SESSION_TOKEN):    # browser login
            return True
        path = urllib.parse.urlparse(self.path).path
        if self.command == "GET" and not path.startswith("/api/") and not path.startswith("/static/"):
            self._redirect("/login?next=" + urllib.parse.quote(self.path))
        else:
            self._send(401, json.dumps({"error": "login required"}), "application/json")
        return False

    def _form(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode() if n else ""
        if self.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(raw or "{}")
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _email_by_token(self, conn, tok):
        return db.one(conn, "SELECT * FROM outreach_emails WHERE tracking_token=?", (tok,))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        conn = db.connect(CFG)
        try:
            if path == "/health":
                return self._send(200, json.dumps({"ok": True, "mail_mode": CFG["mail_mode"]}), "application/json")
            if path.startswith("/t/o/"):
                e = self._email_by_token(conn, path[5:].removesuffix(".gif"))
                if e:
                    db.log_event(conn, e["id"], "open", {"ua": self.headers.get("User-Agent", "")[:120]})
                    conn.commit()
                return self._send(200, PIXEL, "image/gif")
            if path.startswith("/t/c/"):
                tok = path[5:]
                e = self._email_by_token(conn, tok)
                if e:
                    db.log_event(conn, e["id"], "click", {"ua": self.headers.get("User-Agent", "")[:120]})
                    conn.commit()
                return self._redirect(f"/offer/{tok}")
            if path.startswith("/offer/"):
                e = self._email_by_token(conn, path[7:])
                return self._send(200, offer_page(conn, e)) if e else self._send(404, "offer not found")
            if path.startswith("/u/"):
                e = self._email_by_token(conn, path[3:])
                if e:
                    conn.execute("INSERT OR REPLACE INTO suppression(email,reason,ts) VALUES (?,?,?)",
                                 (e["to_email"].lower(), "unsubscribe link", db.now()))
                    db.log_event(conn, e["id"], "unsubscribe", {"via": "link"})
                    conn.commit()
                return self._send(200, "<p style='font-family:sans-serif'>You've been unsubscribed. Sorry to see you go.</p>")
            if path == "/login":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._send(200, login_page((q.get("next") or ["/"])[0]))
            if path == "/logout":
                return self._send(303, "", headers={"Location": "/login", "Set-Cookie": "fd_session=; Path=/; Max-Age=0"})
            if not self._authed():
                return
            if path == "/":
                return self._send(200, chat_page())
            if path == "/overview":
                return self._send(200, overview(conn))
            if path.startswith("/static/"):
                name = os.path.basename(path)
                fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", name)
                if not os.path.isfile(fp):
                    return self._send(404, "not found")
                ctype = "application/javascript" if name.endswith(".js") else "text/css" if name.endswith(".css") else "application/octet-stream"
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), ctype)
            if path == "/traces":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                return self._send(200, traces_page(conn, (q.get("agent") or [None])[0]))
            if path.startswith("/traces/") and path[8:].isdigit():
                pg = trace_page(conn, int(path[8:]))
                return self._send(200, pg) if pg else self._send(404, "no such run")
            if path == "/escalations":
                return self._send(200, escalations_page(conn))
            if path == "/api/sessions":
                return self._send(200, json.dumps(db.rows(conn, """SELECT session_id, MIN(content) first, MAX(id) last
                    FROM (SELECT * FROM chat_messages WHERE role='user' ORDER BY id) GROUP BY session_id
                    ORDER BY last DESC LIMIT 30""")), "application/json")
            if path.startswith("/api/chat/"):
                sid = path[10:]
                return self._send(200, json.dumps(db.rows(conn, "SELECT role, content, mode, run_id, artifacts FROM chat_messages "
                                                               "WHERE session_id=? ORDER BY id", (sid,))), "application/json")
            if path.startswith("/api/runs/") and path[10:].isdigit():
                d = run_detail(conn, int(path[10:]))
                return self._send(200, json.dumps(d, default=str), "application/json") if d else self._send(404, "{}")
            if path == "/api/escalations":
                return self._send(200, json.dumps(db.rows(conn, "SELECT * FROM escalations WHERE status='open' ORDER BY id DESC")),
                                  "application/json")
            if path == "/targets":
                return self._send(200, targets_page(conn))
            if path == "/queue":
                return self._send(200, queue_page(conn))
            if path == "/sent":
                return self._send(200, sent_page(conn))
            if path == "/inbox":
                return self._send(200, inbox_page(conn))
            if path == "/api/report":
                return self._send(200, json.dumps(cli.report(conn), default=str), "application/json")
            if path == "/api/queue":
                return self._send(200, json.dumps(db.rows(conn, "SELECT id,kind,to_email,cc_emails,subject,rationale "
                                  "FROM outreach_emails WHERE status='pending_approval'")), "application/json")
            if path.startswith("/api/email/"):
                return self._send(200, json.dumps(db.one(conn, "SELECT * FROM outreach_emails WHERE id=?",
                                                         (int(path.rsplit('/', 1)[1]),))), "application/json")
            return self._send(404, "not found")
        finally:
            conn.close()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        conn = db.connect(CFG)
        try:
            if path.startswith("/t/convert/"):
                e = self._email_by_token(conn, path[11:])
                if not e:
                    return self._send(404, "not found")
                acct = db.one(conn, "SELECT * FROM accounts WHERE id=?", (e["account_id"],))
                if not conn.execute("SELECT 1 FROM email_events WHERE email_id=? AND type='convert'", (e["id"],)).fetchone():
                    delta = UPGRADE_MRR.get(e["offer_id"], lambda a: 0)(acct)
                    to_plan = TARGET_PLAN.get(e["offer_id"], acct["plan"])
                    conn.execute("INSERT INTO conversions(account_id,email_id,plan_from,plan_to,mrr_delta,ts) VALUES (?,?,?,?,?,?)",
                                 (acct["id"], e["id"], acct["plan"], to_plan, delta, db.now()))
                    conn.execute("UPDATE accounts SET plan=?, mrr=mrr+? WHERE id=?", (to_plan, delta, acct["id"]))
                    db.log_event(conn, e["id"], "convert", {"plan_to": to_plan, "mrr_delta": delta})
                    db.audit(conn, "customer", "converted", {"email_id": e["id"], "account": acct["name"]})
                    conn.commit()
                    traces.escalate("tracking", "deal_won", f"Deal won: {acct['name']} started {to_plan} (+${delta:,.0f} MRR)",
                                    {"next_step": "hand off to account executive for onboarding/contract",
                                     "offer": e["offer_id"]}, "high", email_id=e["id"], account_id=acct["id"],
                                    dedupe_key=f"won-{e['id']}", conn=conn)
                return self._send(200, offer_page(conn, e, done=True))
            if path == "/login":
                f = self._form()
                nxt = f.get("next") or "/"
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = "/"
                ok = hmac.compare_digest((f.get("user") or "").strip(), CFG["dashboard_user"]) and \
                    hmac.compare_digest(f.get("password") or "", CFG["dashboard_password"])
                if not ok:
                    return self._send(401, login_page(nxt, "Wrong username or password."))
                return self._send(303, "", headers={"Location": nxt, "Set-Cookie":
                                  f"fd_session={SESSION_TOKEN}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"})
            if not self._authed():
                return
            parts = path.strip("/").split("/")
            if path == "/api/chat":
                f = self._form()
                msg = (f.get("message") or "").strip()[:4000]
                if not msg:
                    return self._send(400, json.dumps({"error": "empty message"}), "application/json")
                rid = agent.start_chat(f.get("session_id") or "default", msg, f.get("mode") or "harness")
                return self._send(200, json.dumps({"run_id": rid}), "application/json")
            if len(parts) == 3 and parts[0] == "escalations" and parts[1].isdigit() and parts[2] == "resolve":
                f = self._form()
                who = (f.get("who") or CFG["dashboard_user"]).strip()
                note = (f.get("note") or "").strip()
                conn.execute("UPDATE escalations SET status='resolved', resolved_by=?, resolved_at=? WHERE id=?",
                             (who + (f": {note}" if note else ""), db.now(), int(parts[1])))
                db.audit(conn, who, "escalation_resolved", {"id": int(parts[1]), "note": note})
                conn.commit()
                return self._redirect("/escalations")
            if len(parts) == 3 and parts[0] == "queue" and parts[1].isdigit():
                f = self._form()
                eid = int(parts[1])
                approver = (f.get("approver") or CFG["dashboard_user"]).strip()
                if parts[2] == "approve":
                    status, detail = mailer.approve_and_send(conn, eid, approver, f.get("subject"), f.get("body"))
                elif parts[2] == "reject":
                    mailer.reject(conn, eid, approver, f.get("reason", "rejected in dashboard"))
                    status, detail = "rejected", ""
                else:
                    return self._send(404, "unknown action")
                if self.headers.get("Accept", "").startswith("application/json") or \
                        self.headers.get("Content-Type", "").startswith("application/json"):
                    return self._send(200, json.dumps({"id": eid, "status": status, "detail": detail}), "application/json")
                cls = "sent" if status == "sent" else "blocked"
                flash = f"<div class='card {cls}'>#{eid}: {esc(status)} {esc(detail)}</div>"
                return self._send(200, queue_page(conn, flash))
            return self._send(404, "not found")
        finally:
            conn.close()


def main():
    host, port = CFG["dashboard_host"], int(os.environ.get("PORT", CFG["dashboard_port"]))
    conn = db.connect(CFG)
    db.init_schema(conn)
    conn.close()
    print(f"dashboard on http://{host}:{port}  (mail_mode={CFG['mail_mode']}, track_base_url={CFG['track_base_url']})",
          flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    main()
