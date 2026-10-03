"""Offline pipeline tests: synthetic data -> scoring -> drafts -> approval gate -> send (dryrun .eml) -> tracking ->
inbound reply -> classify -> threaded reply draft -> unsubscribe/suppression. LLM is stubbed; mail is dryrun.

  python3 -m unittest tests.test_pipeline -v
"""
import base64
import email
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email import policy
from email.message import EmailMessage

TMP = tempfile.mkdtemp(prefix="outreach-test-")
with open(os.path.join(TMP, "config.json"), "w") as _f:
    json.dump({"allowlist": ["champion.teammate@gmail.com", "teammate@gmail.com"]}, _f)
os.environ.update({"OUTREACH_DB_PATH": os.path.join(TMP, "t.db"), "OUTREACH_MAIL_MODE": "dryrun",
                   "OUTREACH_SENDER_EMAIL": "demo.sender@gmail.com", "OUTREACH_OUTBOX_DIR": os.path.join(TMP, "outbox"), "OUTREACH_TRACK_BASE_URL": "http://track.test",
                   "OUTREACH_CONFIG": os.path.join(TMP, "config.json")})
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from outreach import config, db, inbox, llm, mailer, scoring, templates, writer  # noqa: E402


def fake_chat_json(messages, required=(), **kw):
    sysmsg = messages[0]["content"]
    if "Classify" in sysmsg:
        text = messages[-1]["content"].lower()
        intent = "intro_to_vp" if "vp" in text else "meeting_request" if "call" in text else "interested"
        return {"intent": intent, "sentiment": "positive", "summary": "stub", "needs_reply": True}, "stub", 0.01
    return {"subject": "Stub subject", "opening": "Stub opening.", "value_para": "Stub value.",
            "rationale": "stub rationale"}, "stub-model", 0.01


def fake_chat(messages, **kw):
    return "Hi there,\n\nThursday at 10am works.\n\nMaya", "stub-model", 0.01


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        llm.chat_json = fake_chat_json
        llm.chat = fake_chat
        subprocess.run([sys.executable, os.path.join(ROOT, "data", "generate_data.py"), "--fresh", "--end", "2026-10-03"],
                       check=True, capture_output=True)
        cls.conn = db.connect()
        # route all synthetic recipients to plus-addresses (like `cli map-contacts`)
        cls.conn.execute("UPDATE users SET email = 'demo.sender+u' || id || '@gmail.com'")
        cls.conn.execute("UPDATE accounts SET vp_email = 'demo.sender+vp' || id || '@gmail.com'")
        cls.conn.commit()

    def test_1_data_and_segments(self):
        n = self.conn.execute("SELECT COUNT(DISTINCT date) FROM usage_daily").fetchone()[0]
        self.assertGreaterEqual(n, 360)
        accts = scoring.account_stats(self.conn)
        expect = {"viral_team": "expansion", "free_power": "pro_upsell", "declining": "winback",
                  "dormant": "reactivation", "enterprise": "enterprise_healthy"}
        for a in accts:
            if a["archetype"] in expect:
                self.assertEqual(a["segment"], expect[a["archetype"]], a["name"])
        top = scoring.targets(self.conn, "expansion", 5)
        self.assertTrue(all(a["archetype"] == "viral_team" for a in top))

    def test_2_draft_send_track_reply(self):
        cid, ids = writer.draft(self.conn, "expansion", top=2, log=lambda *_: None)
        self.assertEqual(len(ids), 2)
        self.conn.execute("UPDATE outreach_emails SET to_email='champion.teammate@gmail.com' WHERE id=?", (ids[0],))
        self.conn.commit()
        e = db.one(self.conn, "SELECT * FROM outreach_emails WHERE id=?", (ids[0],))
        self.assertEqual(e["status"], "pending_approval")
        self.assertEqual(templates.unresolved(e["body"]), [])
        facts = json.loads(e["facts_json"])
        self.assertIn(f"{facts['active_users']} people actively using", e["body"])

        # gate 1: cannot send without approval
        status, why = mailer.send(self.conn, ids[1])
        self.assertEqual(status, "blocked")
        self.assertIn("approve", why)

        status, mid = mailer.approve_and_send(self.conn, ids[0], "tester", body=e["body"] + "\nEdited by human.\n")
        self.assertEqual(status, "sent", mid)
        raw = open(os.path.join(config.load()["outbox_dir"], f"{ids[0]}.eml"), "rb").read()
        msg = email.message_from_bytes(raw, policy=policy.default)
        self.assertEqual(msg["X-Outreach-Id"], str(ids[0]))
        text = msg.get_body(("plain",)).get_content()
        self.assertIn(f"http://track.test/t/c/{e['tracking_token']}", text)
        self.assertIn("Edited by human.", text)
        self.assertIn(f"/t/o/{e['tracking_token']}.gif", msg.get_body(("html",)).get_content())

        # inbound reply threaded on our Message-ID, with the VP cc'd
        reply = EmailMessage()
        reply["From"] = f"Champion <{e['to_email']}>"
        reply["To"] = "demo.sender@gmail.com"
        reply["Cc"] = "demo.sender+vp-real@gmail.com"
        reply["Subject"] = "Re: " + e["subject"]
        reply["Message-ID"] = "<reply-1@customer.test>"
        reply["In-Reply-To"] = mid
        reply["References"] = mid
        reply.set_content("Happy to help - looping in our VP. Can we do a call Thursday?\n\nOn Mon someone wrote:\n> old")
        r = inbox.ingest(self.conn, bytes(reply), uid="1", log=lambda *_: None)
        self.assertEqual(r["matched_email_id"], ids[0])
        self.assertEqual(r["intent"], "intro_to_vp")
        rd = db.one(self.conn, "SELECT * FROM outreach_emails WHERE id=?", (r["reply_draft_id"],))
        self.assertEqual((rd["kind"], rd["status"], rd["in_reply_to"]), ("reply", "pending_approval", "<reply-1@customer.test>"))
        self.assertIn("demo.sender+vp-real@gmail.com", rd["cc_emails"])
        self.assertIsNone(inbox.ingest(self.conn, bytes(reply), uid="1"), "duplicate must be skipped")

        status, _ = mailer.approve_and_send(self.conn, rd["id"], "tester")
        self.assertEqual(status, "sent")
        m2 = email.message_from_bytes(open(os.path.join(config.load()["outbox_dir"], f"{rd['id']}.eml"), "rb").read(),
                                      policy=policy.default)
        self.assertEqual(m2["In-Reply-To"], "<reply-1@customer.test>")
        self.assertIn(mid, m2["References"])
        self.assertTrue(m2["Subject"].startswith("Re:"))

    def test_3_allowlist_and_unsubscribe(self):
        cid, ids = writer.draft(self.conn, "winback", top=1, log=lambda *_: None)
        self.conn.execute("UPDATE outreach_emails SET to_email='ceo@realcompany.com' WHERE id=?", (ids[0],))
        self.conn.commit()
        status, why = mailer.approve_and_send(self.conn, ids[0], "tester")
        self.assertEqual(status, "blocked")
        self.assertIn("allowlist", why)
        self.assertFalse(mailer.is_allowed("someone@acme.example"))

        cid, ids = writer.draft(self.conn, "pro_upsell", top=1, log=lambda *_: None)
        e = db.one(self.conn, "SELECT * FROM outreach_emails WHERE id=?", (ids[0],))
        self.assertEqual(mailer.approve_and_send(self.conn, ids[0], "tester")[0], "sent")
        e = db.one(self.conn, "SELECT * FROM outreach_emails WHERE id=?", (ids[0],))
        unsub = EmailMessage()
        unsub["From"], unsub["To"], unsub["Subject"] = e["to_email"], "demo.sender@gmail.com", "Re: x"
        unsub["Message-ID"], unsub["In-Reply-To"] = "<unsub-1@customer.test>", e["message_id"]
        unsub.set_content("Please unsubscribe me.")
        # reply comes from the recipient's plus address -> treated as own mail, so ingest via a different From
        self.conn.execute("UPDATE outreach_emails SET to_email='teammate@gmail.com' WHERE id=?", (ids[0],))
        del unsub["From"]
        unsub["From"] = "teammate@gmail.com"
        r = inbox.ingest(self.conn, bytes(unsub), uid="2", log=lambda *_: None)
        self.assertEqual(r["intent"], "unsubscribe")
        self.assertIsNone(r["reply_draft_id"])
        self.assertTrue(self.conn.execute("SELECT 1 FROM suppression WHERE email='teammate@gmail.com'").fetchone())

    def test_4_dashboard_http(self):
        os.environ["PORT"] = "0"
        import importlib
        sys.path.insert(0, os.path.join(ROOT, "dashboard"))
        server = importlib.import_module("server")
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(base + "/api/queue")
            self.assertEqual(cm.exception.code, 401)
            self.assertIn("Sign in", urllib.request.urlopen(base + "/queue").read().decode())   # redirected to login
            auth = {"Authorization": "Basic " + base64.b64encode(b"admin:outreach").decode()}
            for p in ("/", "/targets", "/queue", "/sent", "/inbox", "/api/report"):
                self.assertEqual(urllib.request.urlopen(urllib.request.Request(base + p, headers=auth)).status, 200, p)
            cid, ids = writer.draft(self.conn, "expansion", top=1, account_ids=None, log=lambda *_: None) \
                if not self.conn.execute("SELECT 1 FROM outreach_emails WHERE status='pending_approval' AND kind='outbound'").fetchone() \
                else (None, [self.conn.execute("SELECT id FROM outreach_emails WHERE status='pending_approval' AND kind='outbound'").fetchone()[0]])
            req = urllib.request.Request(base + f"/queue/{ids[0]}/approve", data=b"approver=http-tester",
                                         headers=dict(auth, Accept="application/json"), method="POST")
            res = json.load(urllib.request.urlopen(req))
            self.assertEqual(res["status"], "sent", res)
            tok = db.one(self.conn, "SELECT tracking_token FROM outreach_emails WHERE id=?", (ids[0],))["tracking_token"]
            urllib.request.urlopen(base + f"/t/o/{tok}.gif").read()
            page = urllib.request.urlopen(base + f"/t/c/{tok}").read().decode()     # follows redirect to /offer
            self.assertIn("Start upgrade", page)
            urllib.request.urlopen(urllib.request.Request(base + f"/t/convert/{tok}", data=b"", method="POST")).read()
            types = {r[0] for r in self.conn.execute("SELECT type FROM email_events WHERE email_id=?", (ids[0],))}
            self.assertTrue({"open", "click", "convert"} <= types, types)
            self.assertTrue(self.conn.execute("SELECT 1 FROM conversions WHERE email_id=?", (ids[0],)).fetchone())
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
