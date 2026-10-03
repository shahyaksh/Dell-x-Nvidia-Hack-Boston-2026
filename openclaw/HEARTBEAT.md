# Heartbeat: outreach inbox check

On each heartbeat:
1. Run `cd /sandbox/AutomatedOutreach && python3 -m outreach.cli inbox` to pick up new replies to outreach emails.
2. Run `python3 -m outreach.cli queue` to list drafts waiting for human approval.
3. If there are new replies or pending drafts, post a short summary: who replied, their intent, and how many drafts await approval at http://127.0.0.1:8090/queue.
4. Never send email and never approve drafts. Only a human approves, in the dashboard.
If nothing changed, reply HEARTBEAT_OK.
