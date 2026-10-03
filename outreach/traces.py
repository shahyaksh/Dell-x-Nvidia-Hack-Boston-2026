"""Agent observability + human escalation.

Every agent (chat orchestrator, analytics, writer, inbox, OpenClaw passthrough) records a run with ordered steps
(LLM calls, tool calls, guard decisions, escalations) so sales can see exactly what the agent did and why.
Anything that needs a human decision is escalated into the dashboard's Escalations inbox.
"""
import json
import time

from . import db


def _j(x, limit=6000):
    if x is None:
        return None
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    return s if len(s) <= limit else s[:limit] + f"... [{len(s) - limit} more chars]"


class Run:
    """A traced agent run. Uses its own short-lived connections so it is safe across threads."""

    def __init__(self, agent, input=None, session_id=None, parent=None, model=None):
        self.agent, self.t0, self.seq = agent, time.time(), 0
        self.last_targets = (None, [])
        with db.connect() as c:
            cur = c.execute("INSERT INTO agent_runs(agent,session_id,parent_id,input,model,started_at) VALUES (?,?,?,?,?,?)",
                            (agent, session_id, parent.id if parent else None, _j(input), model, db.now()))
            self.id = cur.lastrowid

    def step(self, kind, name, input=None, output=None, ms=None, status="ok"):
        self.seq += 1
        with db.connect() as c:
            c.execute("INSERT INTO agent_steps(run_id,seq,kind,name,input,output,status,ms,ts) VALUES (?,?,?,?,?,?,?,?,?)",
                      (self.id, self.seq, kind, name, _j(input), _j(output), status, ms, db.now()))

    def finish(self, output=None, status="ok", model=None):
        with db.connect() as c:
            c.execute("UPDATE agent_runs SET output=?, status=?, ended_at=?, ms=?, model=COALESCE(?, model) WHERE id=?",
                      (_j(output, 20000), status, db.now(), int(1000 * (time.time() - self.t0)), model, self.id))


class NullRun:
    id = None

    def step(self, *a, **k):
        pass

    def finish(self, *a, **k):
        pass


def escalate(source, kind, title, detail=None, severity="medium", email_id=None, account_id=None, run=None,
             dedupe_key=None, conn=None):
    """Create (or refresh) an open escalation for a human. Returns its id."""
    own = conn is None
    c = conn or db.connect()
    try:
        if dedupe_key:
            r = c.execute("SELECT id FROM escalations WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if r:
                c.execute("UPDATE escalations SET title=?, detail=?, severity=?, status='open', created_at=? WHERE id=?",
                          (title, _j(detail), severity, db.now(), r[0]))
                c.commit()
                return r[0]
        cur = c.execute("INSERT INTO escalations(source,kind,severity,title,detail,email_id,account_id,run_id,dedupe_key,"
                        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (source, kind, severity, title, _j(detail), email_id, account_id,
                         run.id if run else None, dedupe_key, db.now()))
        c.commit()
        if run:
            run.step("escalation", kind, None, {"escalation_id": cur.lastrowid, "title": title, "severity": severity})
        return cur.lastrowid
    finally:
        if own:
            c.close()


def resolve_approval_escalations(conn):
    """Auto-close 'approval_needed' escalations once no draft from that batch is still pending."""
    for e in db.rows(conn, "SELECT id, detail FROM escalations WHERE kind='approval_needed' AND status='open'"):
        try:
            ids = json.loads(e["detail"] or "{}").get("draft_ids", [])
        except ValueError:
            ids = []
        if ids and not conn.execute(f"SELECT 1 FROM outreach_emails WHERE status='pending_approval' AND id IN "
                                    f"({','.join('?' * len(ids))})", ids).fetchone():
            conn.execute("UPDATE escalations SET status='resolved', resolved_by='system: all drafts reviewed', "
                         "resolved_at=? WHERE id=?", (db.now(), e["id"]))
    conn.commit()
