"""Chat orchestrator agent (local Nemotron via OpenShell inference.local) for the sales dashboard.

Tools wrap the same capabilities as the OpenClaw skills. The agent can analyze, draft, check the inbox and escalate,
but it has NO tool to send or approve email: that is a human button in the dashboard. Every LLM call and tool
call is recorded as a trace step; charts/tables/drafts are returned as artifacts for the UI.
"""
import json
import os
import subprocess
import threading
import time
import traceback

from . import analytics, cli, config, db, inbox, llm, scoring, templates, traces, writer

SYSTEM = """You are FlowDesk's revenue operations agent, running on a local NVIDIA Nemotron model inside an NVIDIA
OpenShell sandbox. You help salespeople with product analytics, finding upsell/expansion targets, drafting outreach
and handling replies. Today is {today}.

Tools:
- query_analytics: ANY question about users, usage, geography (country/city/region), plans, MRR, features, paywall
  hits, trends, campaign performance. It writes SQL, runs it and returns a chart. Prefer it for data questions.
- find_targets: ranked accounts for a segment (expansion = Enterprise upsell, pro_upsell, winback, reactivation).
- draft_outreach: drafts personalized emails (queued for HUMAN approval; you cannot send).
- approval_queue, check_inbox, campaign_report: status tools.
- escalate_to_human: when a human decision is needed (pricing exceptions, legal/security questions, unhappy
  customers, anything risky or ambiguous) or when the user asks you to flag something.

Rules: if the user asks you to draft/write emails you MUST call draft_outreach (never describe drafts you did not
create); if asked to flag/escalate you MUST call escalate_to_human. Never claim an email was sent - say drafts are waiting for approval in the Approval queue. Quote numbers only
from tool results. Keep answers short and skimmable (bullets ok). After drafting, tell the user to review the queue."""

TOOLS = [
    {"type": "function", "function": {
        "name": "query_analytics",
        "description": "Answer a product-analytics question with SQL over 12 months of usage, accounts, geography, "
                       "billing and campaign data. Returns rows, a chart and an explanation.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}}},
    {"type": "function", "function": {
        "name": "find_targets", "description": "Rank accounts for an outreach segment using deterministic scoring.",
        "parameters": {"type": "object", "properties": {
            "segment": {"type": "string", "enum": ["expansion", "pro_upsell", "winback", "reactivation"]},
            "top": {"type": "integer", "description": "how many accounts (default 5)"},
            "region": {"type": "string", "enum": ["NA", "EMEA", "APAC", "LATAM"], "description": "optional region filter"}},
            "required": ["segment"]}}},
    {"type": "function", "function": {
        "name": "draft_outreach",
        "description": "Draft personalized outreach emails for top accounts in a segment (or specific account ids). "
                       "Drafts go to the human approval queue; nothing is sent.",
        "parameters": {"type": "object", "properties": {
            "segment": {"type": "string", "enum": ["expansion", "pro_upsell", "winback", "reactivation"]},
            "top": {"type": "integer"},
            "template": {"type": "string", "enum": list(templates.T)},
            "audience": {"type": "string", "enum": ["champion", "vp"]},
            "account_ids": {"type": "array", "items": {"type": "integer"},
                            "description": "ids from find_targets; omit to use the accounts just found"},
            "region": {"type": "string", "enum": ["NA", "EMEA", "APAC", "LATAM"]}},
            "required": ["segment"]}}},
    {"type": "function", "function": {
        "name": "approval_queue", "description": "List drafts waiting for human approval.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "check_inbox", "description": "Poll Gmail for replies to outreach; classify and draft replies for approval.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "campaign_report", "description": "Funnel (sent/opened/clicked/replied/converted) and MRR uplift, "
                                                  "live campaigns vs last year's baseline.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "escalate_to_human", "description": "Flag something for a human in the Escalations inbox.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "detail": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "account_id": {"type": "integer"}}, "required": ["title", "detail"]}}},
]


# ------------------------------------------------------------------ tool implementations
def t_query_analytics(run, artifacts, question):
    sub = traces.Run("analytics-agent", question, session_id=None, parent=run)
    try:
        r = analytics.answer(question, sub)
        sub.finish(r["explanation"])
    except Exception as e:
        sub.finish(repr(e), "error")
        return f"Analytics failed: {e}"
    artifacts.append({"type": "chart", "title": r["chart"].get("title") or question, "chart": r["chart"], "data": r["data"],
                      "columns": r["columns"], "rows": r["rows"][:50], "sql": r["sql"], "explanation": r["explanation"],
                      "run_id": sub.id})
    return json.dumps({"columns": r["columns"], "first_rows": r["rows"][:15], "row_count": r["row_count"],
                       "explanation": r["explanation"], "note": "chart already shown to the user"}, default=str)


def t_find_targets(run, artifacts, segment, top=5, region=None):
    with db.connect() as conn:
        accts = [a for a in scoring.account_stats(conn) if a["segment"] == segment and (not region or a["region"] == region)]
    accts = sorted(accts, key=lambda a: -a["score"])[:max(1, min(int(top or 5), 15))]
    run.last_targets = (segment, [a["id"] for a in accts])
    rows = [[a["id"], a["name"], a["region"], a["plan"], a["score"], f"{a['active_users_30']}/{a['seats_purchased']}",
             a["power_users"], f"{round(a['growth'] * 100):+d}%", a["paywall_90"], (a["champion"] or {}).get("name"),
             a["vp_name"]] for a in accts]
    cols = ["id", "account", "region", "plan", "score", "active/seats", "power users", "QoQ", "paywall hits", "champion", "VP"]
    artifacts.append({"type": "table", "title": f"Top {segment} targets" + (f" in {region}" if region else ""),
                      "columns": cols, "rows": rows})
    return json.dumps([{"id": a["id"], "summary": scoring.summary_line(a)} for a in accts])


def t_draft_outreach(run, artifacts, segment, top=3, template=None, audience=None, account_ids=None, region=None):
    top = min(int(top or 3), 8)
    if not account_ids and region:
        with db.connect() as conn:
            accts = [a for a in scoring.account_stats(conn) if a["segment"] == segment and a["region"] == region]
        account_ids = [a["id"] for a in sorted(accts, key=lambda a: -a["score"])[:top]]
    if not account_ids and getattr(run, "last_targets", (None,))[0] == segment:
        account_ids = run.last_targets[1][:top]          # default to the accounts the agent just showed the user
        run.step("info", "draft-scope", None, f"using accounts from find_targets: {account_ids}")
    with db.connect() as conn:
        cid, ids = writer.draft(conn, segment, top, template, audience, account_ids=account_ids or None,
                                log=lambda s: run.step("info", "writer", None, s), run=run)
        drafts = db.rows(conn, f"SELECT e.id, a.name account, e.to_email, e.subject, e.rationale, e.model FROM "
                               f"outreach_emails e JOIN accounts a ON a.id=e.account_id WHERE e.id IN "
                               f"({','.join('?' * len(ids)) or 'NULL'})", ids)
    artifacts.append({"type": "drafts", "title": f"{len(ids)} drafts waiting for approval", "drafts": drafts})
    return json.dumps({"campaign_id": cid, "drafts_pending_approval": drafts,
                       "next": "human reviews in the Approval queue"})


def t_approval_queue(run, artifacts):
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT e.id, e.kind, a.name account, e.to_email, e.subject, e.rationale FROM outreach_emails e "
                             "LEFT JOIN accounts a ON a.id=e.account_id WHERE e.status='pending_approval' ORDER BY e.id")
    if rows:
        artifacts.append({"type": "drafts", "title": f"{len(rows)} drafts waiting for approval", "drafts": rows})
    return json.dumps(rows)


def t_check_inbox(run, artifacts):
    cfg = config.load()
    if cfg["mail_mode"] != "gmail":
        return "Inbox polling is disabled (mail_mode=dryrun)."
    with db.connect() as conn:
        res = inbox.poll(conn, log=lambda s: run.step("info", "inbox", None, s))
    return json.dumps({"new_replies": res})


def t_campaign_report(run, artifacts):
    with db.connect() as conn:
        r = cli.report(conn)
    keys = ["sent", "opened", "clicked", "replied", "converted"]
    src = [s for s in ("historical", "live") if s in r["by_source"]]
    artifacts.append({"type": "chart", "title": "Funnel: last year's blasts vs live agent campaigns (% of sent)",
                      "chart": {"type": "bar", "x": "stage", "y": src},
                      "data": {"labels": keys, "datasets": [
                          {"label": s, "data": [round(100 * r["by_source"][s][k] / max(r["by_source"][s]["sent"], 1), 1)
                                                for k in keys]} for s in src]}})
    return json.dumps({"by_source": r["by_source"], "mrr_by_source": r["mrr_by_source"],
                       "inbound_intents": r["inbound_intents"]})


def t_escalate(run, artifacts, title, detail, severity="medium", account_id=None):
    eid = traces.escalate("chat-agent", "agent_request", title, detail, severity, account_id=account_id, run=run)
    artifacts.append({"type": "escalation", "title": title, "id": eid, "severity": severity})
    return json.dumps({"escalation_id": eid, "status": "open - a human will pick it up in the Escalations inbox"})


IMPL = {"query_analytics": t_query_analytics, "find_targets": t_find_targets, "draft_outreach": t_draft_outreach,
        "approval_queue": t_approval_queue, "check_inbox": t_check_inbox, "campaign_report": t_campaign_report,
        "escalate_to_human": t_escalate}


# ------------------------------------------------------------------ orchestrator loop
def _history(session_id, limit=8):
    with db.connect() as conn:
        rows = db.rows(conn, "SELECT role, content FROM chat_messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                       (session_id, limit))
    return [{"role": r["role"], "content": r["content"][:2000]} for r in reversed(rows)]


ACTION_TOOLS = [  # (regex on the user's request, tool that must have been called before answering)
    (r"\b(draft|write|prepare)\b.*\b(email|emails|outreach|intro|intros|message)", "draft_outreach"),
    (r"\b(escalat\w*|flag)\b", "escalate_to_human"),
    (r"\b(inbox|replies|replied|responses)\b", "check_inbox"),
]


def missing_actions(message, called):
    import re
    return [tool for rx, tool in ACTION_TOOLS if re.search(rx, message, re.I) and tool not in called]


def run_harness(session_id, message, run, max_steps=7):
    with db.connect() as conn:
        today = conn.execute("SELECT MAX(date) FROM usage_daily").fetchone()[0]
    messages = [{"role": "system", "content": SYSTEM.format(today=today)}] + _history(session_id) + \
               [{"role": "user", "content": message}]
    artifacts, model, called, nudged = [], None, set(), False
    for _ in range(max_steps):
        msg, model, _ = llm.chat_tools(messages, TOOLS, run=run)
        messages.append(msg)
        if not msg.get("tool_calls"):
            missing = missing_actions(message, called)
            if missing and not nudged:      # action-verification guard: don't let the model claim work it didn't do
                nudged = True
                run.step("guard", "action-verification", {"requested": missing, "called": sorted(called)},
                         "answer rejected: requested action not performed; asking the agent to call the tool", status="error")
                messages.append({"role": "user", "content": f"You have not actually called {', '.join(missing)} yet. "
                                                            "Call it now with the right arguments, then answer."})
                continue
            return msg["content"] or "(no answer)", artifacts, model
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except ValueError:
                args = {}
            t0 = time.time()
            try:
                out = IMPL[name](run, artifacts, **args) if name in IMPL else f"unknown tool {name}"
                status = "ok"
            except Exception as e:
                out, status = f"ERROR: {e}", "error"
            run.step("tool", name, args, out, int(1000 * (time.time() - t0)), status)
            called.add(name)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(out)[:6000]})
    return "I stopped after several tool steps; here is what I found so far.", artifacts, model


def run_openclaw(session_id, message, run):
    """Pass the turn to the NemoClaw/OpenClaw `main` agent and import its session transcript as trace steps."""
    sid = f"ui-{session_id}"
    run.step("info", "openclaw", {"session": sid}, "openclaw agent --agent main (inside this OpenShell sandbox)")
    p = subprocess.run(["openclaw", "agent", "--agent", "main", "--session-id", sid, "-m", message, "--json"],
                       capture_output=True, text=True, timeout=600)
    text = p.stdout.strip()
    answer = text
    try:
        j = json.loads(text[text.index("{"):])
        answer = j.get("reply") or j.get("text") or j.get("message") or \
            "\n".join(x.get("text", "") for x in (j.get("payloads") or j.get("result", {}).get("payloads") or []) if isinstance(x, dict)) or text
    except ValueError:
        pass
    sess = os.path.expanduser(f"/sandbox/.openclaw/agents/main/sessions/{sid}.jsonl")
    if os.path.exists(sess):
        for line in open(sess):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") or {}
            content = m.get("content") if isinstance(m.get("content"), list) else []
            for c in content:
                if c.get("type") == "toolCall":
                    run.step("tool", f"openclaw:{c.get('name')}", c.get("arguments"), None)
            if m.get("role") == "toolResult":
                run.step("info", "openclaw:tool-result", None, " ".join(c.get("text", "") for c in content)[:3000])
    if p.returncode != 0:
        run.step("error", "openclaw", None, p.stderr[-2000:], status="error")
    return answer or p.stderr[-500:], [], "openclaw/main"


def start_chat(session_id, message, mode="harness"):
    """Record the user message, start the agent in a background thread, return run id (UI polls the trace)."""
    run = traces.Run("chat-agent" if mode == "harness" else "openclaw-agent", message, session_id=session_id)
    with db.connect() as conn:
        conn.execute("INSERT INTO chat_messages(session_id, role, content, mode, run_id, ts) VALUES (?,?,?,?,?,?)",
                     (session_id, "user", message, mode, run.id, db.now()))

    def work():
        try:
            fn = run_harness if mode == "harness" else run_openclaw
            answer, artifacts, model = fn(session_id, message, run)
            run.finish(answer, model=model)
        except Exception as e:
            answer, artifacts = f"Agent error: {e}", []
            run.step("error", "exception", None, traceback.format_exc()[-3000:], status="error")
            run.finish(answer, "error")
        with db.connect() as conn:
            conn.execute("INSERT INTO chat_messages(session_id, role, content, mode, run_id, artifacts, ts) "
                         "VALUES (?,?,?,?,?,?,?)", (session_id, "assistant", answer, mode, run.id,
                                                    json.dumps(artifacts, default=str), db.now()))
    threading.Thread(target=work, daemon=True).start()
    return run.id
