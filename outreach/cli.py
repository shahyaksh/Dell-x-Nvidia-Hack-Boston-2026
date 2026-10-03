"""Outreach harness CLI (what the OpenClaw skills call).

  python3 -m outreach.cli init [--seed 42]                 generate the 12-month dataset (fresh DB)
  python3 -m outreach.cli map-contacts                     route synthetic recipients to safe real inboxes
  python3 -m outreach.cli analyze --segment expansion --top 10 [--json]
  python3 -m outreach.cli draft --segment expansion --top 5 [--template vp_enterprise_pitch] [--account-id N]
  python3 -m outreach.cli queue [--json]                   drafts waiting for human approval
  python3 -m outreach.cli inbox                            poll Gmail once: match, classify, draft replies
  python3 -m outreach.cli watch --every 60                 inbox loop
  python3 -m outreach.cli report [--json]                  funnel per campaign / segment
  python3 -m outreach.cli egress-check                     prove the sandbox can only reach allowed hosts
There is deliberately NO send/approve command: sending happens only from the dashboard after human approval.
"""
import argparse
import json
import os
import subprocess
import sys
import time

from . import config, db, inbox, scoring, templates, writer


def cmd_init(a):
    gen = os.path.join(config.ROOT, "data", "generate_data.py")
    subprocess.run([sys.executable, gen, "--fresh", "--seed", str(a.seed)] + (["--end", a.end] if a.end else []),
                   check=True)


def cmd_map_contacts(a):
    cfg = config.load()
    conn = db.connect(cfg)
    sender = config.sender_email(cfg)
    local, domain = sender.split("@", 1)
    if domain.endswith("example.com") and cfg["mail_mode"] == "gmail":
        sys.exit("set sender_email or gmail_config first")
    conn.execute("UPDATE users SET email = ? || '+u' || id || '@' || ?", (local, domain))
    conn.execute("UPDATE accounts SET vp_email = ? || '+vp' || id || '@' || ?", (local, domain))
    demo = cfg.get("demo_contacts") or {}
    top = scoring.targets(conn, "expansion", 1)
    if top and (demo.get("champion") or demo.get("vp")):
        acct = top[0]
        if demo.get("champion"):
            conn.execute("UPDATE users SET email=? WHERE id=?", (demo["champion"], acct["champion"]["user_id"]))
        if demo.get("vp"):
            conn.execute("UPDATE accounts SET vp_email=? WHERE id=?", (demo["vp"], acct["id"]))
        print(f"demo account: {acct['name']} (id {acct['id']}) champion={demo.get('champion')} vp={demo.get('vp')}")
    db.audit(conn, "operator", "map_contacts", {"sender": sender})
    conn.commit()
    print(f"all synthetic recipients now route to {local}+...@{domain}")


def cmd_analyze(a):
    conn = db.connect()
    accts = scoring.targets(conn, a.segment, a.top)
    if a.json:
        out = []
        for x in accts:
            f = scoring.facts(x, "champion")
            f["summary"] = scoring.summary_line(x)
            out.append(f)
        print(json.dumps(out, indent=2, default=str))
        return
    seg_counts = {}
    for x in scoring.account_stats(conn):
        seg_counts[x["segment"]] = seg_counts.get(x["segment"], 0) + 1
    print("segments:", seg_counts)
    print(f"top {a.segment} targets:")
    for i, x in enumerate(accts, 1):
        print(f"{i:2d}. [acct {x['id']}] {scoring.summary_line(x)}")


def cmd_draft(a):
    conn = db.connect()
    cid, ids = writer.draft(conn, a.segment, a.top, a.template, a.audience, account_ids=a.account_id or None)
    print(json.dumps({"campaign_id": cid, "drafts_pending_approval": ids,
                      "next": "a human must review and approve these in the dashboard (/queue)"}))


def cmd_queue(a):
    conn = db.connect()
    rows = db.rows(conn, "SELECT id, kind, to_email, subject, model, rationale, created_at FROM outreach_emails "
                         "WHERE status='pending_approval' ORDER BY id")
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"#{r['id']} [{r['kind']}] -> {r['to_email']}: {r['subject']}\n    why: {r['rationale']}")
        print(f"{len(rows)} pending approval")


def cmd_inbox(a):
    conn = db.connect()
    res = inbox.poll(conn)
    print(json.dumps({"new_replies": res}, indent=2))


def cmd_watch(a):
    conn = db.connect()
    print(f"watching inbox every {a.every}s (Ctrl-C to stop)", flush=True)
    while True:
        try:
            res = inbox.poll(conn, log=lambda s: print(time.strftime("%H:%M:%S"), s, flush=True))
            if not res:
                print(time.strftime("%H:%M:%S"), "no new replies", flush=True)
        except Exception as e:
            print(time.strftime("%H:%M:%S"), "poll error:", repr(e), flush=True)
        time.sleep(a.every)


FUNNEL_SQL = """
SELECT c.id campaign_id, c.name, c.segment, c.source,
  COUNT(DISTINCT CASE WHEN e.status='sent' THEN e.id END) sent,
  COUNT(DISTINCT CASE WHEN ev.type='open' THEN e.id END) opened,
  COUNT(DISTINCT CASE WHEN ev.type='click' THEN e.id END) clicked,
  COUNT(DISTINCT CASE WHEN ev.type='reply' THEN e.id END) replied,
  COUNT(DISTINCT CASE WHEN ev.type='convert' THEN e.id END) converted,
  COUNT(DISTINCT CASE WHEN e.status='pending_approval' THEN e.id END) pending
FROM campaigns c LEFT JOIN outreach_emails e ON e.campaign_id=c.id AND e.kind='outbound'
LEFT JOIN email_events ev ON ev.email_id=e.id
GROUP BY c.id ORDER BY c.created_at DESC"""


def report(conn):
    camps = db.rows(conn, FUNNEL_SQL)
    agg = {}
    for c in camps:
        g = agg.setdefault(c["source"], dict(sent=0, opened=0, clicked=0, replied=0, converted=0))
        for k in g:
            g[k] += c[k]
    for g in agg.values():
        s = g["sent"] or 1
        g.update(open_rate=round(100 * g["opened"] / s, 1), click_rate=round(100 * g["clicked"] / s, 1),
                 reply_rate=round(100 * g["replied"] / s, 1), conversion_rate=round(100 * g["converted"] / s, 1))
    mrr = db.rows(conn, "SELECT c.source, ROUND(SUM(v.mrr_delta),0) mrr FROM conversions v JOIN outreach_emails e "
                        "ON e.id=v.email_id JOIN campaigns c ON c.id=e.campaign_id GROUP BY 1")
    status = db.rows(conn, "SELECT status, COUNT(*) n FROM outreach_emails WHERE model != 'generic-template' "
                           "OR model IS NULL GROUP BY 1")
    intents = db.rows(conn, "SELECT intent, COUNT(*) n FROM inbound_messages GROUP BY 1")
    return {"by_source": agg, "mrr_by_source": mrr, "live_status": status, "inbound_intents": intents,
            "campaigns": camps}


def cmd_report(a):
    r = report(db.connect())
    if a.json:
        print(json.dumps(r, indent=2))
        return
    for src, g in r["by_source"].items():
        print(f"{src:10s} sent={g['sent']} open={g['open_rate']}% click={g['click_rate']}% reply={g['reply_rate']}% "
              f"convert={g['conversion_rate']}%")
    print("MRR uplift:", r["mrr_by_source"])
    print("live email status:", r["live_status"])
    print("inbound intents:", r["inbound_intents"])


def cmd_stats(a):
    """Machine-readable state for scripts/e2e_test.sh."""
    conn = db.connect()
    live = "e.model IS NOT 'generic-template'"
    out = {
        "usage": db.one(conn, "SELECT COUNT(*) rows_, COUNT(DISTINCT date) days, COUNT(DISTINCT user_id) users FROM usage_daily"),
        "emails": db.rows(conn, f"SELECT e.kind, e.status, COUNT(*) n FROM outreach_emails e WHERE {live} GROUP BY 1, 2"),
        "events": db.rows(conn, f"SELECT v.type, COUNT(*) n FROM email_events v JOIN outreach_emails e ON e.id=v.email_id "
                                f"WHERE {live} GROUP BY 1"),
        "inbound": db.rows(conn, "SELECT id, from_email, intent, matched_email_id FROM inbound_messages ORDER BY id"),
        "suppression": [r["email"] for r in db.rows(conn, "SELECT email FROM suppression")],
        "conversions": db.rows(conn, "SELECT c.* FROM conversions c JOIN outreach_emails e ON e.id=c.email_id WHERE " + live),
        "recent": db.rows(conn, f"SELECT id, kind, status, status_detail, to_email, cc_emails, subject, message_id, in_reply_to, "
                                f"body LIKE '%${{%' leftover FROM outreach_emails e WHERE {live} ORDER BY id DESC LIMIT 20"),
        "demo_account": None,
    }
    cfg = config.load()
    champ = (cfg.get("demo_contacts") or {}).get("champion")
    if champ:
        out["demo_account"] = db.one(conn, "SELECT a.id, a.name, a.plan FROM users u JOIN accounts a ON a.id=u.account_id "
                                           "WHERE lower(u.email)=lower(?)", (champ,))
    print(json.dumps(out, indent=2, default=str))


def cmd_escalate(a):
    from . import traces
    eid = traces.escalate(a.source, "agent_request", a.title, a.detail, a.severity, account_id=a.account_id)
    print(json.dumps({"escalation_id": eid, "status": "open - visible to humans in the dashboard Escalations inbox"}))


def cmd_egress(a):
    import socket
    import urllib.request
    cfg = config.load()
    checks = [("inference", cfg["llm_base_url"].rstrip("/") + "/models"), ("github", "https://github.com/"),
              ("pypi", "https://pypi.org/simple/"), ("example", "https://example.com/")]
    for name, url in checks:
        try:
            st = urllib.request.urlopen(url, timeout=8).status
            print(f"  REACHABLE  {st}  {url}")
        except Exception as e:
            print(f"  BLOCKED    {type(e).__name__}: {str(e)[:60]}  {url}")
    from . import net
    for host, port in (("smtp.gmail.com", 465), ("imap.gmail.com", 993), ("smtp.office365.com", 587)):
        try:
            print(f"  REACHABLE  tls  {host}:{port}  banner={net.tls_probe(host, port)[:40]!r}")
        except Exception as e:
            print(f"  BLOCKED    {type(e).__name__}: {str(e)[:70]}  {host}:{port}")


def main():
    ap = argparse.ArgumentParser(prog="outreach", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--seed", type=int, default=42); p.add_argument("--end"); p.set_defaults(fn=cmd_init)
    p = sub.add_parser("map-contacts"); p.set_defaults(fn=cmd_map_contacts)
    p = sub.add_parser("analyze"); p.add_argument("--segment", default="expansion", choices=list(templates.SEGMENT_TEMPLATE))
    p.add_argument("--top", type=int, default=10); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_analyze)
    p = sub.add_parser("draft"); p.add_argument("--segment", default="expansion", choices=list(templates.SEGMENT_TEMPLATE))
    p.add_argument("--top", type=int, default=5); p.add_argument("--template", choices=list(templates.T))
    p.add_argument("--audience", choices=["champion", "vp"]); p.add_argument("--account-id", type=int, action="append")
    p.set_defaults(fn=cmd_draft)
    p = sub.add_parser("queue"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_queue)
    p = sub.add_parser("inbox"); p.set_defaults(fn=cmd_inbox)
    p = sub.add_parser("watch"); p.add_argument("--every", type=int, default=60); p.set_defaults(fn=cmd_watch)
    p = sub.add_parser("report"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_report)
    p = sub.add_parser("egress-check"); p.set_defaults(fn=cmd_egress)
    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("escalate"); p.add_argument("--title", required=True); p.add_argument("--detail", default="")
    p.add_argument("--severity", default="medium", choices=["low", "medium", "high"]); p.add_argument("--account-id", type=int)
    p.add_argument("--source", default="openclaw-agent"); p.set_defaults(fn=cmd_escalate)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
