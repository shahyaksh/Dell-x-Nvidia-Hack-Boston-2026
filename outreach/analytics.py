"""Product-analytics agent: natural-language question -> SQL (local LLM) -> read-only execution -> chart spec ->
grounded explanation. Self-repairs SQL errors; every step is traced."""
import json
import re
import sqlite3
import time

from . import config, db, llm

SCHEMA_DOC = """SQLite database for the B2B SaaS product "FlowDesk". Today is {today}; data covers the last 365 days.

accounts(id, name, domain, industry, employees, plan ['free','pro','team','enterprise'], seats_purchased, mrr (USD/month),
         signup_date, vp_name, vp_title, vp_email, country, city, region ['NA','EMEA','APAC','LATAM'])
users(id, account_id -> accounts.id, name, email, title, created_at, last_seen, country, city, region)
usage_daily(user_id -> users.id, date 'YYYY-MM-DD', sessions, minutes, actions, reports, api_calls, automations,
            collab_invites, paywall_hits, paywall_feature)   -- one row per user per ACTIVE day (no row = inactive)
invoices(account_id, month 'YYYY-MM', plan, seats, amount)
campaigns(id, name, segment, template_id, offer_id, created_at, source ['historical','live'])
outreach_emails(id, campaign_id, kind ['outbound','reply'], account_id, user_id, to_email, subject, status
                ['pending_approval','approved','sent','rejected','failed','blocked'], template_id, offer_id, model, sent_at)
email_events(email_id -> outreach_emails.id, type ['open','click','reply','unsubscribe','convert'], ts)
conversions(account_id, email_id, plan_from, plan_to, mrr_delta, ts)
inbound_messages(from_email, subject, intent, sentiment, matched_email_id, received_at)

Definitions:
- active user in a period = user with at least one usage_daily row in that period.
- weekly buckets: strftime('%Y-%W', date); monthly: substr(date,1,7).
- "last 30 days": date > date('{today}', '-30 day').
- paywall hits = SUM(paywall_hits); always add "paywall_feature IS NOT NULL" when grouping by paywall_feature.
- growth % = 100.0 * (recent - previous) / NULLIF(previous, 0).
- prefer GROUP BY with COUNT(DISTINCT user_id) for user counts.
- AVOID JOIN FAN-OUT: aggregate usage per account/user in a CTE first, then join that CTE to accounts. Never SUM
  account-level columns (mrr, seats_purchased) after joining accounts to users or usage_daily.
- account-level questions (MRR, plan, seats) only need the accounts table.
- paywall_feature values: sso, audit_log, advanced_permissions, seat_limit, api_rate_limit, export_limit,
  automation_limit, history_limit, api_access, shared_workspaces, admin_console.
"""

SQL_PROMPT = SCHEMA_DOC + """
Write ONE SQLite SELECT (or WITH ... SELECT) query that answers the user's question. Return at most 50 rows unless
it is a time series. Use readable column aliases. Then choose a chart.
Return JSON: {{"sql": "...", "chart": {{"type": "bar"|"line"|"pie"|"table", "x": "<column for x axis/labels>",
"y": ["<numeric column>", ...], "title": "..."}}, "assumptions": "one short sentence"}}"""

EXPLAIN_PROMPT = """You are a product analyst. Explain the query result to a salesperson in 3-5 short sentences:
the direct answer first, then the most notable pattern, then one sales action it suggests. Use ONLY numbers that
appear in the result rows. Plain text, no markdown tables."""


def _today(conn):
    return conn.execute("SELECT MAX(date) FROM usage_daily").fetchone()[0]


def _readonly_conn(cfg):
    conn = sqlite3.connect(f"file:{cfg['db_path']}?mode=ro", uri=True, timeout=10)
    conn.execute("PRAGMA temp_store=MEMORY")

    def authorizer(action, *args):  # belt and braces on top of mode=ro
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
    conn.set_authorizer(authorizer)
    deadline = [time.time() + 20]
    conn.set_progress_handler(lambda: 1 if time.time() > deadline[0] else 0, 10000)
    return conn, deadline


def _check_sql(sql):
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        raise ValueError("only one statement is allowed")
    if not re.match(r"(?is)^\s*(select|with)\b", s):
        raise ValueError("only SELECT queries are allowed")
    return s


def _fix_chart(chart, cols, rows):
    chart = dict(chart or {})
    numeric = [c for i, c in enumerate(cols) if rows and all(isinstance(r[i], (int, float)) or r[i] is None for r in rows)]
    if chart.get("x") in numeric and any(c not in numeric for c in cols):
        chart["x"] = None                    # model put a measure on the x axis; use the label column instead
    if chart.get("x") not in cols:
        chart["x"] = next((c for c in cols if c not in numeric), cols[0])
    ys = [y for y in (chart.get("y") or []) if y in numeric and y != chart["x"]] if isinstance(chart.get("y"), list) \
        else [chart.get("y")] if chart.get("y") in numeric else []
    chart["y"] = ys or [c for c in numeric if c != chart["x"]][:3]
    if chart.get("type") not in ("bar", "line", "pie", "table") or not chart["y"]:
        chart["type"] = "table" if not chart["y"] else "bar"
    if chart["type"] == "pie" and (len(chart["y"]) > 1 or len(rows) > 12):
        chart["type"] = "bar"
    chart.setdefault("title", "")
    return chart


def datasets(chart, cols, rows):
    """Chart.js-ready data. Long-format results (label, series, value) are pivoted into one dataset per series."""
    if chart["type"] == "table" or not rows:
        return None
    xi = cols.index(chart["x"])
    numeric = {c for i, c in enumerate(cols) if all(isinstance(r[i], (int, float)) or r[i] is None for r in rows)}
    labels_cols = [c for c in cols if c not in chart["y"] and c != chart["x"] and c not in numeric]
    if len(cols) == 3 and labels_cols and len(chart["y"]) == 1:      # pivot: (series, x, value) long format
        si, yi = cols.index(labels_cols[0]), cols.index(chart["y"][0])
        xs = sorted({r[xi] for r in rows}, key=lambda v: (v is None, v))
        series = {}
        for r in rows:
            series.setdefault(str(r[si]), {})[r[xi]] = r[yi]
        return {"labels": [str(x) for x in xs],
                "datasets": [{"label": k, "data": [v.get(x) for x in xs]} for k, v in list(series.items())[:8]]}
    return {"labels": [str(r[xi]) for r in rows[:200]],
            "datasets": [{"label": y, "data": [r[cols.index(y)] for r in rows[:200]]} for y in chart["y"]]}


def answer(question, run, max_repairs=3):
    cfg = config.load()
    with db.connect(cfg) as c:
        today = _today(c)
    messages = [{"role": "system", "content": SQL_PROMPT.format(today=today)},
                {"role": "user", "content": question}]
    ro, deadline = _readonly_conn(cfg)
    spec = cols = rows = sql = None
    try:
        for attempt in range(max_repairs + 1):
            spec, _, _ = llm.chat_json(messages, required=("sql",), run=run, name=f"text-to-sql#{attempt + 1}",
                                       temperature=0)
            try:
                sql = _check_sql(spec["sql"])
                t0 = time.time()
                deadline[0] = t0 + 20          # query time budget (not counting LLM time)
                cur = ro.execute(sql)
                cols = [d[0] for d in cur.description]
                rows = cur.fetchmany(500)
                run.step("tool", "sqlite (read-only)", sql, {"columns": cols, "row_count": len(rows), "first_rows": rows[:5]},
                         int(1000 * (time.time() - t0)))
                if not rows and attempt < max_repairs:
                    raise ValueError("query returned 0 rows; check filters/value spellings (e.g. country names, plans)")
                if len(rows) > 1 and len(set(map(tuple, rows))) < len(rows) and attempt < max_repairs:
                    raise ValueError("result has duplicate rows - a JOIN fan-out; aggregate in a CTE before joining")
                break
            except (sqlite3.Error, ValueError) as e:
                run.step("guard", "sql-repair", sql or spec.get("sql"), f"{type(e).__name__}: {e}", status="error")
                if attempt == max_repairs:
                    raise
                messages += [{"role": "assistant", "content": json.dumps(spec)},
                             {"role": "user", "content": f"That query failed: {e}. Re-read the schema: only usage_daily has "
                                                         "a `date` column (alias it, e.g. ud.date, and join usage_daily ud ON "
                                                         "ud.user_id = u.id, users u ON u.account_id = a.id). Every column "
                                                         "used in an outer query must be selected in the CTE. Return the "
                                                         "same JSON format."}]
    finally:
        ro.close()
    chart = _fix_chart(spec.get("chart"), cols, rows)
    sample = [dict(zip(cols, r)) for r in rows[:40]]
    explanation, _, _ = llm.chat(
        [{"role": "system", "content": EXPLAIN_PROMPT},
         {"role": "user", "content": f"Question: {question}\nSQL: {sql}\nRows ({len(rows)} total, first 40): "
                                     f"{json.dumps(sample, default=str)}"}], max_tokens=400, temperature=0.2,
        run=run, name="explain-result")
    return {"question": question, "sql": sql, "columns": cols, "data": datasets(chart, cols, rows), "rows": [list(r) for r in rows[:200]],
            "row_count": len(rows), "chart": chart, "assumptions": spec.get("assumptions"), "explanation": explanation}
