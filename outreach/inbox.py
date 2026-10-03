"""Inbox agent: poll Gmail IMAP, match replies to our outreach, classify intent, draft a threaded reply for approval.

Replies NEVER auto-send: they land in the dashboard queue as kind='reply', status='pending_approval'.
`ingest()` is transport-agnostic so tests can feed raw RFC822 bytes without IMAP.
"""
import datetime as dt
import email
import email.utils
import json
import re
import secrets
from email import policy
from email.utils import getaddresses, parseaddr

from . import config, db, llm, net, traces

INTENTS = ("interested", "meeting_request", "intro_to_vp", "pricing_question", "objection", "not_now",
           "unsubscribe", "out_of_office", "other")

CLASSIFY = """Classify a customer's reply to a sales/customer-success email from {product}.
Return JSON: {{"intent": one of {intents}, "sentiment": "positive"|"neutral"|"negative",
"summary": one sentence, "needs_reply": true|false}}.
Use "intro_to_vp" when they loop in / offer to introduce a manager or executive.
Use "meeting_request" when they propose or ask for a call/meeting time."""

REPLY = """You are {sender_name} ({sender_title}) at {product}. Draft a reply to the customer's email below.
Rules: use only the facts given; do not invent prices, discounts, dates or features beyond the offer text;
keep it under 140 words; plain text; warm and specific; if they proposed a time, accept it or propose two
concrete alternatives; if they introduced a VP/executive, thank them and address the executive too;
answer pricing questions using only the offer text and offer a call for details.
Do not include a subject line. End with your name. Return only the email body."""


def _text_body(msg):
    if msg.is_multipart():
        part = msg.get_body(preferencelist=("plain", "html"))
        content = part.get_content() if part else ""
        if part is not None and part.get_content_type() == "text/html":
            content = re.sub(r"<[^>]+>", " ", content)
    else:
        content = msg.get_content()
    # drop quoted history ("On ... wrote:" and "> " lines)
    content = re.split(r"\n\s*On .{5,200}wrote:\s*\n", content, maxsplit=1)[0]
    return "\n".join(l for l in content.splitlines() if not l.startswith(">")).strip()


def _local_iso(date_hdr):
    """Email Date header -> local naive ISO string comparable with db.now() timestamps."""
    try:
        return email.utils.parsedate_to_datetime(str(date_hdr)).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def classify(text, subject="", run=None):
    cfg = config.load()
    low = text.lower()
    if re.search(r"\b(unsubscribe|remove me|stop emailing|opt out)\b", low):
        return {"intent": "unsubscribe", "sentiment": "negative", "summary": "Asked to unsubscribe.",
                "needs_reply": False}, "rules", 0.0
    try:
        data, model, secs = llm.chat_json(
            [{"role": "system", "content": CLASSIFY.format(product=cfg["product_name"], intents=list(INTENTS))},
             {"role": "user", "content": f"Subject: {subject}\n\n{text[:4000]}"}], required=("intent",),
            temperature=0, run=run, name="classify-intent")
        if data["intent"] not in INTENTS:
            data["intent"] = "other"
        return data, model, secs
    except Exception as e:
        return {"intent": "other", "sentiment": "neutral", "summary": f"(classifier unavailable: {e})",
                "needs_reply": True}, "fallback", 0.0


def match(conn, in_reply_to, references, from_addr, outreach_id=None, received=None):
    ids = re.findall(r"<[^>]+>", f"{in_reply_to or ''} {references or ''}")
    for mid in reversed(ids):
        r = db.one(conn, "SELECT * FROM outreach_emails WHERE message_id=?", (mid,))
        if r:
            return r
    if outreach_id and str(outreach_id).isdigit():
        r = db.one(conn, "SELECT * FROM outreach_emails WHERE id=?", (int(outreach_id),))
        if r:
            return r
    if ids:
        return None          # explicit reply to a thread we don't know (e.g. an older campaign) -> ignore
    # fallback (no threading headers): latest email we sent to this address BEFORE the reply arrived
    r = db.one(conn, "SELECT * FROM outreach_emails WHERE lower(to_email)=? AND status='sent' "
                     "ORDER BY sent_at DESC LIMIT 1", (from_addr.lower(),))
    if r and received is not None and r["sent_at"] and received < r["sent_at"]:
        return None          # reply predates our email: belongs to an older thread
    return r


def draft_reply(conn, inbound, original, cls, run=None):
    cfg = config.load()
    facts = json.loads(original["facts_json"]) if original and original.get("facts_json") else {}
    offer = db.one(conn, "SELECT headline, details FROM offers WHERE id=?", (original["offer_id"],)) if original else None
    ctx = (f"Our original email:\nSubject: {original['subject']}\n{original['body'][:2500]}\n\n" if original else "") + \
          (f"Offer text: {offer['headline']} - {offer['details']}\n" if offer else "") + \
          (f"Account facts (JSON): {json.dumps(facts)}\n" if facts else "") + \
          f"Classified intent: {cls['intent']} ({cls.get('summary', '')})\n" + \
          f"Customer {inbound['from_name'] or ''} <{inbound['from_email']}> wrote" + \
          (f" (cc: {inbound['cc_emails']})" if inbound["cc_emails"] else "") + f":\n{inbound['body'][:3000]}"
    try:
        body, model, secs = llm.chat(
            [{"role": "system", "content": REPLY.format(sender_name=cfg["sender_name"], sender_title=cfg["sender_title"],
                                                        product=cfg["product_name"])},
             {"role": "user", "content": ctx}], max_tokens=600, temperature=0.4, run=run, name="draft-reply")
    except Exception as e:
        body, model, secs = (f"Hi {(inbound['from_name'] or 'there').split()[0]},\n\nThanks for getting back to me - "
                             f"I'll follow up shortly.\n\n{cfg['sender_name']}"), "fallback", 0.0
    subject = inbound["subject"] or (original["subject"] if original else "")
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    refs = " ".join(x for x in [inbound["references_hdr"], inbound["message_id"]] if x).strip()
    sender = config.sender_email(cfg).lower()
    cc = ", ".join(a for a in (inbound["cc_emails"] or "").split(", ") if a and a.lower() != sender) or None
    cur = conn.execute(
        "INSERT INTO outreach_emails(campaign_id,kind,parent_id,inbound_id,account_id,user_id,to_email,cc_emails,"
        "subject,body,status,offer_id,model,llm_seconds,rationale,facts_json,in_reply_to,references_hdr,"
        "tracking_token,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (original["campaign_id"] if original else None, "reply", original["id"] if original else None, inbound["id"],
         original["account_id"] if original else None, original["user_id"] if original else None,
         inbound["from_email"], cc, subject, body.strip() + "\n", "pending_approval",
         original["offer_id"] if original else None, model, secs,
         f"Reply to {cls['intent']} ({cls.get('sentiment')}): {cls.get('summary', '')}",
         original["facts_json"] if original else None, inbound["message_id"], refs, secrets.token_urlsafe(12),
         db.now()))
    return cur.lastrowid


def ingest(conn, raw, uid=None, log=print):
    """Process one raw RFC822 message. Returns dict summary or None if skipped."""
    cfg = config.load()
    msg = email.message_from_bytes(raw, policy=policy.default)
    mid = (msg["Message-ID"] or "").strip() or f"<no-id-{uid}@local>"
    if conn.execute("SELECT 1 FROM inbound_messages WHERE message_id=?", (mid,)).fetchone():
        return None
    from_name, from_addr = parseaddr(msg["From"] or "")
    from_addr = from_addr.lower()
    sender = config.sender_email(cfg).lower()
    if from_addr == sender or from_addr.startswith(sender.split("@")[0] + "+"):
        return None   # our own mail (sent copies / plus-address loopback)
    cc = ", ".join(a for _, a in getaddresses(msg.get_all("Cc", [])) if a)
    body = _text_body(msg)
    original = match(conn, msg["In-Reply-To"], msg["References"], from_addr, msg["X-Outreach-Id"],
                     received=_local_iso(msg["Date"]))
    if original is None:
        # not a reply to our outreach -> record but don't act (no spam auto-replies)
        conn.execute("INSERT INTO inbound_messages(message_id,imap_uid,from_email,from_name,cc_emails,subject,body,"
                     "in_reply_to,references_hdr,intent,received_at,processed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (mid, uid, from_addr, from_name, cc, msg["Subject"], None, msg["In-Reply-To"],
                      msg["References"], "unmatched", db.now(), db.now()))
        conn.commit()
        return {"message_id": mid, "from": from_addr, "intent": "unmatched"}
    conn.commit()
    run = traces.Run("inbox-agent", {"from": from_addr, "subject": msg["Subject"]})
    run.step("tool", "match-thread", {"in_reply_to": msg["In-Reply-To"]}, {"matched_email_id": original["id"],
                                                                            "to": original["to_email"]})
    cls, model, secs = classify(body, msg["Subject"] or "", run=run)
    cur = conn.execute(
        "INSERT INTO inbound_messages(message_id,imap_uid,from_email,from_name,cc_emails,subject,body,in_reply_to,"
        "references_hdr,matched_email_id,intent,sentiment,summary,received_at,processed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, uid, from_addr, from_name, cc, msg["Subject"], body[:20000], msg["In-Reply-To"], msg["References"],
         original["id"], cls["intent"], cls.get("sentiment"), cls.get("summary"), db.now(), db.now()))
    inbound = db.one(conn, "SELECT * FROM inbound_messages WHERE id=?", (cur.lastrowid,))
    db.log_event(conn, original["id"], "reply", {"inbound_id": inbound["id"], "intent": cls["intent"]})
    reply_id = None
    if cls["intent"] == "unsubscribe":
        conn.execute("INSERT OR REPLACE INTO suppression(email,reason,ts) VALUES (?,?,?)",
                     (from_addr, "replied unsubscribe", db.now()))
        db.log_event(conn, original["id"], "unsubscribe", {"via": "reply"})
    elif cls["intent"] != "out_of_office" and cls.get("needs_reply", True) is not False:
        conn.commit()
        reply_id = draft_reply(conn, inbound, original, cls, run=run)
    db.audit(conn, "inbox-agent", "ingested", {"inbound_id": inbound["id"], "matched": original["id"],
                                               "intent": cls["intent"], "reply_draft": reply_id})
    conn.commit()
    hot = {"intro_to_vp": "high", "meeting_request": "high", "pricing_question": "high", "interested": "medium",
           "objection": "medium", "unsubscribe": "low"}
    if cls["intent"] in hot:
        traces.escalate("inbox-agent", "hot_lead" if hot[cls["intent"]] == "high" else cls["intent"],
                        f"{cls['intent'].replace('_', ' ')} from {from_name or from_addr}",
                        {"summary": cls.get("summary"), "reply_draft_id": reply_id, "inbound_id": inbound["id"]},
                        hot[cls["intent"]], email_id=reply_id or original["id"], account_id=original["account_id"],
                        run=run, dedupe_key=f"inbound-{inbound['id']}")
    run.finish(cls | {"reply_draft_id": reply_id}, model=model)
    out = {"message_id": mid, "from": from_addr, "matched_email_id": original["id"], "intent": cls["intent"],
           "summary": cls.get("summary"), "reply_draft_id": reply_id, "classifier": model}
    log(f"inbound from {from_addr}: intent={cls['intent']} matched=#{original['id']} reply_draft={reply_id}")
    return out


_SKIPPED = set()   # UIDs already checked and found unrelated (per watcher process)


def poll(conn, log=print):
    """Privacy-preserving poll: read HEADERS only; fetch a full message only if it replies to our outreach."""
    cfg = config.load()
    user, pw = config.gmail_credentials(cfg)
    since = (dt.date.today() - dt.timedelta(days=cfg["imap_lookback_days"])).strftime("%d-%b-%Y")
    sender = user.lower()
    results = []
    with net.IMAP4_SSL("imap.gmail.com", 993, timeout=30) as box:
        box.login(user, pw)
        for folder, tag in (("INBOX", "in"), ('"[Gmail]/Spam"', "spam")):   # new threads often land in Spam
            if box.select(folder, readonly=True)[0] != "OK":
                continue
            status, data = box.uid("search", None, f'(SINCE "{since}")')
            if status != "OK":
                raise RuntimeError(f"IMAP search failed in {folder}")
            for uid in data[0].split():
                u = f"{tag}:{uid.decode()}"
                if u in _SKIPPED or conn.execute("SELECT 1 FROM inbound_messages WHERE imap_uid=?", (u,)).fetchone():
                    continue
                status, hdr = box.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM DATE IN-REPLY-TO REFERENCES X-OUTREACH-ID)])")
                raw_h = next((x[1] for x in hdr if isinstance(x, tuple)), b"")
                h = email.message_from_bytes(raw_h, policy=policy.default)
                from_addr = parseaddr(h["From"] or "")[1].lower()
                if from_addr == sender or from_addr.startswith(sender.split("@")[0] + "+") or \
                        not match(conn, h["In-Reply-To"], h["References"], from_addr, h["X-Outreach-Id"],
                                  received=_local_iso(h["Date"])):
                    _SKIPPED.add(u)          # unrelated personal mail: never downloaded or stored
                    continue
                status, msg_data = box.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((x[1] for x in msg_data if isinstance(x, tuple)), None)
                if status == "OK" and raw:
                    r = ingest(conn, raw, uid=u, log=log)
                    if r:
                        r["folder"] = folder
                        results.append(r)
    return results
