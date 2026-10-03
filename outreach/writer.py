"""Writer agent: template + deterministic facts -> local LLM personalization -> pending_approval draft."""
import json
import re
import secrets

from . import config, db, llm, scoring, templates, traces

SYSTEM = """You are a senior B2B SaaS customer-success writer for {product}. You personalize outreach emails.
Rules:
- Use ONLY the facts provided. Never invent numbers, customers, features, prices or deadlines.
- Warm, specific, concise, no hype, no emojis. Plain text.
- Do NOT include a greeting ("Hi ...") or a sign-off; the template already has them.
- The template ALREADY lists every statistic in bullet points. Do NOT put any digits/statistics in "opening" or
  "value_para"; describe the situation qualitatively (e.g. "your team has outgrown its seats").
- "opening": 1-2 sentences addressed to the recipient, referencing their role/team and what the data shows.
- "value_para": at most 2 sentences connecting their specific locked features / usage pattern to the offer benefits.
- "subject": under 70 characters, specific to the account, no clickbait.
- "rationale": one sentence for the human reviewer: why this account and this angle.
Return a JSON object with keys: subject, opening, value_para, rationale."""


FALLBACK = {  # safe, number-free copy used when the LLM fails or keeps inventing numbers
    "expansion": ("Your team at {account_name} has clearly made FlowDesk part of how it works.",
                  "Enterprise removes the seat and permission limits your team keeps running into."),
    "pro_upsell": ("You've been getting a lot out of FlowDesk on the free plan.",
                   "Pro removes the limits you keep running into."),
    "winback": ("I wanted to check in on how FlowDesk is working for {account_name}.",
                "If something isn't working for your team, we'd like to help fix it."),
    "reactivation": ("Thanks again for signing up for FlowDesk.",
                     "Most teams get value within the first week once their data is connected."),
}


def fallback(f, tpl_id):
    opening, value = FALLBACK[templates.T[tpl_id]["segment"]]
    return {"subject": None, "opening": opening.format(**f), "value_para": value.format(**f),
            "rationale": f"Rule-based fallback for {tpl_id} (LLM output rejected or unavailable)."}


NUM = re.compile(r"\d+(?:\.\d+)?")
GREETING = re.compile(r"^\s*(hi|hello|hey|dear)\b[^,\n]*[,!]?\s*", re.I)


def unsupported_numbers(data, facts_text, offer_text):
    """Hallucination guard: paragraphs may only use numbers from the offer text (stats live in the template
    bullets); the subject may also use numbers from the facts."""
    def nums(t):
        return set(NUM.findall((t or "").replace(",", "")))
    bad = nums(" ".join(str(data.get(k) or "") for k in ("opening", "value_para"))) - nums(offer_text)
    bad |= nums(data.get("subject")) - nums(facts_text + offer_text)
    return sorted(bad)


def personalize(f, tpl_id, run=None):
    cfg = config.load()
    tpl = templates.T[tpl_id]
    offer = templates.OFFERS[tpl["offer"]]
    facts = {k: v for k, v in f.items() if k not in ("to_email", "vp_email", "user_id", "account_id")}
    default_subject = templates.render(tpl["subject"], f)
    user = (f"Template: {tpl_id} - {tpl['description']}\nAudience: {tpl['audience']}\n"
            f"Offer: {offer['headline']} - {offer['details']}\nFacts (JSON): {json.dumps(facts)}\n"
            f"Default subject: {default_subject}")
    facts_text, offer_text = json.dumps(facts) + default_subject, offer["headline"] + " " + offer["details"]
    messages = [{"role": "system", "content": SYSTEM.format(product=cfg["product_name"])},
                {"role": "user", "content": user}]
    total, notes = 0.0, []
    try:
        for attempt in range(2):
            data, model, secs = llm.chat_json(messages, required=("opening", "value_para"), run=run,
                                              name=f"personalize {f['account_name']}")
            total += secs
            data["opening"] = GREETING.sub("", data["opening"]).strip()
            bad = unsupported_numbers(data, facts_text, offer_text)
            if not bad:
                if notes:
                    data["rationale"] = (data.get("rationale") or "") + " [guard: " + "; ".join(notes) + "]"
                return data, model, round(total, 2)
            notes.append(f"attempt {attempt + 1} invented numbers {bad}")
            if run:
                run.step("guard", "hallucination-guard", {"account": f["account_name"]}, notes[-1], status="error")
            messages += [{"role": "assistant", "content": json.dumps(data)},
                         {"role": "user", "content": f"Your opening/value_para/subject used numbers {bad}. The template already "
                                                     "lists the statistics: rewrite the JSON with NO statistics in opening and "
                                                     "value_para (offer terms are fine)."}]
        raise ValueError("; ".join(notes))
    except Exception as e:  # keep the pipeline moving; reviewer sees the fallback rationale
        d = fallback(f, tpl_id)
        d["rationale"] += f" ({e})"
        return d, "fallback", round(total, 2)


def create_campaign(conn, name, segment, tpl_id):
    cur = conn.execute("INSERT INTO campaigns(name,segment,template_id,offer_id,created_at,source) VALUES (?,?,?,?,?,?)",
                       (name, segment, tpl_id, templates.T[tpl_id]["offer"], db.now(), "live"))
    return cur.lastrowid


def draft(conn, segment, top=5, tpl_id=None, audience=None, campaign_name=None, account_ids=None, log=print, run=None):
    cfg = config.load()
    own_run = run is None
    conn.commit()   # trace steps write on their own connection; never hold a write txn across LLM calls
    run = run or traces.Run("writer-agent", {"segment": segment, "top": top, "template": tpl_id, "audience": audience,
                                             "account_ids": account_ids})
    tpl_id = tpl_id or templates.SEGMENT_TEMPLATE[segment]
    tpl = templates.T[tpl_id]
    audience = audience or tpl["audience"]
    if account_ids:
        accts = [a for a in scoring.account_stats(conn) if a["id"] in set(account_ids)]
    else:
        accts = scoring.targets(conn, segment, top)
    cid = create_campaign(conn, campaign_name or f"{segment} / {tpl_id} ({db.now()[:16]})", segment, tpl_id)
    conn.commit()
    created = []
    for a in accts:
        f = scoring.facts(a, audience)
        if not f["to_email"]:
            log(f"skip {a['name']}: no {audience} email")
            continue
        if conn.execute("SELECT 1 FROM outreach_emails WHERE to_email=? AND status IN ('pending_approval','approved')",
                        (f["to_email"],)).fetchone():
            log(f"skip {a['name']}: draft already pending for {f['to_email']}")
            continue
        p, model, secs = personalize(f, tpl_id, run)
        if model == "fallback":
            traces.escalate("writer-agent", "guard_fallback", f"Check wording: LLM output rejected for {a['name']}",
                            p.get("rationale"), "low", account_id=a["id"], run=run)
        values = dict(f, opening=p["opening"].strip(), value_para=p["value_para"].strip(),
                      sender_name=cfg["sender_name"], sender_title=cfg["sender_title"])
        subject = (p.get("subject") or "").strip() or templates.render(tpl["subject"], values)
        body = templates.render(tpl["body"], values)
        cur = conn.execute(
            "INSERT INTO outreach_emails(campaign_id,kind,account_id,user_id,to_email,subject,body,status,template_id,"
            "offer_id,model,llm_seconds,rationale,facts_json,tracking_token,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, "outbound", a["id"], f["user_id"], f["to_email"], subject, body, "pending_approval", tpl_id,
             tpl["offer"], model, secs, p.get("rationale"), json.dumps(f), secrets.token_urlsafe(12), db.now()))
        conn.commit()
        created.append(cur.lastrowid)
        log(f"draft #{cur.lastrowid} -> {f['to_email']} ({a['name']}) [{model}, {secs}s] {subject}")
    db.audit(conn, "writer-agent", "draft_campaign", {"campaign_id": cid, "segment": segment, "template": tpl_id,
                                                      "drafts": created})
    conn.commit()
    if created:
        traces.escalate("writer-agent", "approval_needed", f"{len(created)} {segment} draft(s) need approval",
                        {"campaign_id": cid, "draft_ids": created, "template": tpl_id}, "medium", run=run,
                        dedupe_key=f"approve-campaign-{cid}")
    if own_run:
        run.finish({"campaign_id": cid, "drafts": created})
    return cid, created
