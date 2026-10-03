#!/usr/bin/env python3
"""Plays the CUSTOMER from a real Gmail account (runs on the host, not in the sandbox) so the E2E needs no human.

  customer_sim.py find   --to capcool79@gmail.com [--subject-has X]      print the latest outreach email delivered there
  customer_sim.py engage --to ...                                          open pixel + click tracked link + press upgrade
  customer_sim.py reply  --to ... --body "..." [--cc a@b]                  threaded reply via Gmail SMTP
Creds: ~/gmail_customer.json {"email","app_password"}; sender filter: config.sandbox.json sender_email.
"""
import argparse
import email
import imaplib
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from email import policy
from email.message import EmailMessage
from email.utils import make_msgid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS = json.load(open(os.path.expanduser(os.environ.get("CUSTOMER_CREDS", "~/gmail_customer.json"))))
CFG = json.load(open(os.path.join(HERE, "config.sandbox.json")))
SENDER = CFG["sender_email"]


def latest(to, subject_has=None, wait=300):
    """Most recent mail from SENDER delivered to `to` (INBOX or Spam). Waits for delivery."""
    t0 = time.time()
    while True:
        with imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30) as box:
            box.login(CREDS["email"], CREDS["app_password"])
            for folder in ("INBOX", '"[Gmail]/Spam"'):
                if box.select(folder, readonly=True)[0] != "OK":
                    continue
                st, data = box.uid("search", None, f'(FROM "{SENDER}" TO "{to}" SINCE "{time.strftime("%d-%b-%Y")}")')
                for uid in reversed(data[0].split()):
                    _, md = box.uid("fetch", uid, "(BODY.PEEK[])")
                    msg = email.message_from_bytes(next(x[1] for x in md if isinstance(x, tuple)), policy=policy.default)
                    if to.lower() not in (msg["To"] or "").lower():
                        continue
                    if subject_has and subject_has.lower() not in (msg["Subject"] or "").lower():
                        continue
                    msg.folder = folder
                    return msg
        if time.time() - t0 > wait:
            sys.exit(f"no email from {SENDER} to {to} after {wait}s")
        time.sleep(10)


def summary(msg):
    text = msg.get_body(("plain",)).get_content()
    return {"folder": msg.folder, "subject": msg["Subject"], "message_id": msg["Message-ID"], "to": msg["To"],
            "cc": msg["Cc"], "x_outreach_id": msg["X-Outreach-Id"],
            "links": re.findall(r"https?://\S+/(?:t/c|u)/\S+", text), "preview": text[:300]}


def engage(msg):
    html = msg.get_body(("html",)).get_content()
    pixel = re.search(r'src="([^"]+/t/o/[^"]+\.gif)"', html).group(1)
    click = re.search(r"(https?://\S+/t/c/[\w-]+)", msg.get_body(("plain",)).get_content()).group(1)
    ua = {"User-Agent": "Mozilla/5.0 (customer-sim)"}
    urllib.request.urlopen(urllib.request.Request(pixel, headers=ua), timeout=30).read()
    print("opened  ->", pixel)
    page = urllib.request.urlopen(urllib.request.Request(click, headers=ua), timeout=30)
    print("clicked ->", click, "landed on", page.geturl())
    convert = page.geturl().replace("/offer/", "/t/convert/")
    out = urllib.request.urlopen(urllib.request.Request(convert, data=b"", headers=ua, method="POST"), timeout=30).read()
    print("upgrade ->", convert, "|", "all set" in out.decode().lower() and "confirmed" or "unexpected page")


def reply(msg, body, cc=None):
    r = EmailMessage()
    r["From"] = CREDS["email"]
    r["To"] = SENDER
    if cc:
        r["Cc"] = cc
    subj = msg["Subject"] or ""
    r["Subject"] = subj if subj.lower().startswith("re:") else "Re: " + subj
    r["Message-ID"] = make_msgid(domain="gmail.com")
    r["In-Reply-To"] = msg["Message-ID"]
    r["References"] = " ".join(x for x in (msg["References"], msg["Message-ID"]) if x)
    r.set_content(body + "\n\nOn " + (msg["Date"] or "") + ", " + (msg["From"] or "") + " wrote:\n> " +
                  "\n> ".join(msg.get_body(("plain",)).get_content().splitlines()[:8]))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(CREDS["email"], CREDS["app_password"])
        s.send_message(r)
    print("replied ->", r["Subject"], "| in-reply-to", msg["Message-ID"], "| cc", cc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["find", "engage", "reply"])
    ap.add_argument("--to", required=True)
    ap.add_argument("--subject-has")
    ap.add_argument("--body")
    ap.add_argument("--cc")
    ap.add_argument("--wait", type=int, default=300)
    a = ap.parse_args()
    msg = latest(a.to, a.subject_has, a.wait)
    print(json.dumps(summary(msg), indent=2))
    if a.action == "engage":
        engage(msg)
    elif a.action == "reply":
        reply(msg, a.body, a.cc)


if __name__ == "__main__":
    main()
