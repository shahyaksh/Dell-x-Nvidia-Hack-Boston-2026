---
name: inbox-responder
description: Check the Gmail inbox for replies to outreach emails, classify intent (interested, meeting request, intro to VP, pricing, objection, unsubscribe), and prepare threaded reply drafts for human approval. Use when the user asks about replies, the inbox, responses, or follow-ups.
---

# Inbox Responder

You monitor replies to our outreach. Gmail is reachable from this sandbox only through the NemoClaw `gmail`
policy preset (python3 -> imap.gmail.com:993 / smtp.gmail.com:465). You **never send replies yourself**: reply
drafts are queued for a human in the dashboard (`http://127.0.0.1:8090/queue`).

Poll the inbox once:

```bash
cd /sandbox/AutomatedOutreach && python3 -m outreach.cli inbox
```

The output lists each new reply with its matched outreach email, intent, one-line summary and the id of the
reply draft. Unsubscribe requests are added to the suppression list automatically and get no reply.

Then run `python3 -m outreach.cli queue` and summarize for the user:
- who replied, and their intent and summary;
- which reply drafts are waiting for approval;
- any unsubscribes.

A background watcher (`python3 -m outreach.cli watch --every 60`) may already be polling. If so, `inbox` simply reports nothing new.

## Escalate to a human
Some things need a person to decide: pricing exceptions, security or legal questions, unhappy customers, or anything
you are unsure about. Escalate those instead of acting:
`cd /sandbox/AutomatedOutreach && python3 -m outreach.cli escalate --title "..." --detail "..." --severity high|medium|low [--account-id N]`
Escalations appear in the dashboard's Escalations inbox (http://127.0.0.1:8090/escalations).
