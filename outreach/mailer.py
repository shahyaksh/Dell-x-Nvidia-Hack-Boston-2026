"""Mailer: the ONLY code path that sends email. Called by the dashboard's Approve & Send handler.

Guards (all enforced here, regardless of caller):
  1. status must be 'approved' (a human approved it in the dashboard)
  2. recipient (and every CC) must be on the demo allowlist (or a sender+tag plus-address)
  3. recipient must not be suppressed (unsubscribed)
Tracking: open pixel, tracked offer link, unsubscribe link, X-Outreach-Id, stored Message-ID for reply matching.
MAIL_MODE=dryrun writes .eml files to data/outbox/ instead of using SMTP (unit tests).
"""
import html
import os
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import config, db, net, traces


class Blocked(Exception):
    pass


def is_allowed(addr, cfg=None):
    cfg = cfg or config.load()
    addr = (addr or "").strip().lower()
    if not addr or addr.endswith(".example"):
        return False
    allow = {a.strip().lower() for a in cfg.get("allowlist", [])}
    allow |= {v.strip().lower() for v in (cfg.get("demo_contacts") or {}).values() if v}
    if addr in allow:
        return True
    sender = config.sender_email(cfg).lower()
    if cfg.get("allow_sender_plus") and "@" in sender:
        local, domain = sender.split("@", 1)
        a_local, _, a_domain = addr.partition("@")
        return a_domain == domain and (a_local == local or a_local.startswith(local + "+"))
    return False


def links(cfg, token):
    base = cfg["track_base_url"].rstrip("/")
    return {"offer": f"{base}/t/c/{token}", "unsub": f"{base}/u/{token}", "pixel": f"{base}/t/o/{token}.gif"}


def build_message(cfg, e):
    sender = config.sender_email(cfg)
    lk = links(cfg, e["tracking_token"])
    text = e["body"].replace("{{OFFER_LINK}}", lk["offer"]).replace("{{UNSUB_LINK}}", lk["unsub"])
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["sender_name"], sender))
    msg["To"] = e["to_email"]
    if e.get("cc_emails"):
        msg["Cc"] = e["cc_emails"]
    msg["Subject"] = e["subject"]
    msg["Message-ID"] = e.get("message_id") or make_msgid(domain=sender.split("@")[-1])
    msg["X-Outreach-Id"] = str(e["id"])
    msg["List-Unsubscribe"] = f"<{lk['unsub']}>"
    if e.get("in_reply_to"):
        msg["In-Reply-To"] = e["in_reply_to"]
        msg["References"] = e.get("references_hdr") or e["in_reply_to"]
    msg.set_content(text)
    body_html = html.escape(text).replace(html.escape(lk["offer"]), f'<a href="{lk["offer"]}">{lk["offer"]}</a>') \
        .replace(html.escape(lk["unsub"]), f'<a href="{lk["unsub"]}">unsubscribe</a>')
    msg.add_alternative(f'<div style="font-family:Arial,sans-serif;font-size:14px;white-space:pre-wrap">{body_html}</div>'
                        f'<img src="{lk["pixel"]}" width="1" height="1" alt="" style="display:none">', subtype="html")
    return msg


def send(conn, email_id, actor="dashboard"):
    """Send one APPROVED email. Returns final status. Never raises for policy blocks (records them)."""
    cfg = config.load()
    e = db.one(conn, "SELECT * FROM outreach_emails WHERE id=?", (email_id,))
    if not e:
        raise KeyError(email_id)
    try:
        if e["status"] != "approved":
            raise Blocked(f"status is {e['status']!r}, not 'approved' - a human must approve first")
        rcpts = [e["to_email"]] + [c.strip() for c in (e["cc_emails"] or "").split(",") if c.strip()]
        for r in rcpts:
            if not is_allowed(r, cfg):
                raise Blocked(f"{r} is not on the demo recipient allowlist")
            if conn.execute("SELECT 1 FROM suppression WHERE email=?", (r.lower(),)).fetchone():
                raise Blocked(f"{r} is suppressed (unsubscribed)")
    except Blocked as b:
        if e["status"] in ("approved", "pending_approval"):
            conn.execute("UPDATE outreach_emails SET status='blocked', status_detail=? WHERE id=?", (str(b), email_id))
        db.audit(conn, actor, "send_blocked", {"email_id": email_id, "reason": str(b)})
        conn.commit()
        traces.escalate("mailer", "blocked_send", f"Send blocked for #{email_id} to {e['to_email']}", str(b), "medium",
                        email_id=email_id, account_id=e["account_id"], dedupe_key=f"blocked-{email_id}", conn=conn)
        return "blocked", str(b)

    msg = build_message(cfg, e)
    try:
        if cfg["mail_mode"] == "gmail":
            user, pw = config.gmail_credentials(cfg)
            for attempt in range(3):          # the OpenShell proxy occasionally refuses a CONNECT; retry briefly
                try:
                    with net.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
                        smtp.login(user, pw)
                        smtp.send_message(msg)
                    break
                except ConnectionRefusedError:
                    if attempt == 2:
                        raise
                    import time
                    time.sleep(2 * (attempt + 1))
        else:
            os.makedirs(cfg["outbox_dir"], exist_ok=True)
            with open(os.path.join(cfg["outbox_dir"], f"{email_id}.eml"), "wb") as f:
                f.write(bytes(msg))
    except Exception as ex:
        conn.execute("UPDATE outreach_emails SET status='failed', status_detail=? WHERE id=?", (repr(ex), email_id))
        db.audit(conn, actor, "send_failed", {"email_id": email_id, "error": repr(ex)})
        conn.commit()
        traces.escalate("mailer", "send_failed", f"Send failed for #{email_id}", repr(ex), "high", email_id=email_id,
                        dedupe_key=f"failed-{email_id}", conn=conn)
        return "failed", repr(ex)
    conn.execute("UPDATE outreach_emails SET status='sent', sent_at=?, message_id=?, status_detail=? WHERE id=?",
                 (db.now(), msg["Message-ID"], cfg["mail_mode"], email_id))
    db.audit(conn, actor, "sent", {"email_id": email_id, "to": e["to_email"], "mode": cfg["mail_mode"]})
    conn.commit()
    traces.resolve_approval_escalations(conn)
    return "sent", msg["Message-ID"]


def approve_and_send(conn, email_id, approver, subject=None, body=None):
    e = db.one(conn, "SELECT status FROM outreach_emails WHERE id=?", (email_id,))
    if not e or e["status"] != "pending_approval":
        return "error", f"email {email_id} is not pending approval"
    if subject is not None:
        conn.execute("UPDATE outreach_emails SET subject=? WHERE id=?", (subject, email_id))
    if body is not None:
        conn.execute("UPDATE outreach_emails SET body=? WHERE id=?", (body.replace("\r\n", "\n"), email_id))
    conn.execute("UPDATE outreach_emails SET status='approved', approved_by=?, approved_at=? WHERE id=?",
                 (approver, db.now(), email_id))
    db.audit(conn, approver, "approved", {"email_id": email_id, "edited": body is not None or subject is not None})
    conn.commit()
    return send(conn, email_id, actor=approver)


def reject(conn, email_id, approver, reason=""):
    conn.execute("UPDATE outreach_emails SET status='rejected', status_detail=?, approved_by=? WHERE id=? "
                 "AND status='pending_approval'", (reason, approver, email_id))
    db.audit(conn, approver, "rejected", {"email_id": email_id, "reason": reason})
    conn.commit()
    traces.resolve_approval_escalations(conn)
