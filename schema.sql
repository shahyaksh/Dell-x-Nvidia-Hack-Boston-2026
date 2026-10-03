-- Outreach harness schema (SQLite). One file: product usage + outreach + tracking.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
  id              INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  domain          TEXT NOT NULL,
  industry        TEXT,
  employees       INTEGER,
  plan            TEXT NOT NULL CHECK (plan IN ('free','pro','team','enterprise')),
  seats_purchased INTEGER NOT NULL DEFAULT 1,
  mrr             REAL NOT NULL DEFAULT 0,
  signup_date     TEXT NOT NULL,
  vp_name         TEXT,
  vp_title        TEXT,
  vp_email        TEXT,
  archetype       TEXT,             -- ground truth from the generator (used only by tests)
  country         TEXT,
  city            TEXT,
  region          TEXT              -- NA | EMEA | APAC | LATAM
);

CREATE TABLE IF NOT EXISTS users (
  id          INTEGER PRIMARY KEY,
  account_id  INTEGER NOT NULL REFERENCES accounts(id),
  name        TEXT NOT NULL,
  email       TEXT NOT NULL UNIQUE,
  title       TEXT,
  created_at  TEXT NOT NULL,
  last_seen   TEXT,
  country     TEXT,
  city        TEXT,
  region      TEXT
);

-- One row per user per ACTIVE day (sparse). 12 months of history.
CREATE TABLE IF NOT EXISTS usage_daily (
  user_id         INTEGER NOT NULL REFERENCES users(id),
  date            TEXT NOT NULL,
  sessions        INTEGER NOT NULL,
  minutes         INTEGER NOT NULL,
  actions         INTEGER NOT NULL,
  reports         INTEGER NOT NULL DEFAULT 0,
  api_calls       INTEGER NOT NULL DEFAULT 0,
  automations     INTEGER NOT NULL DEFAULT 0,
  collab_invites  INTEGER NOT NULL DEFAULT 0,
  paywall_hits    INTEGER NOT NULL DEFAULT 0,
  paywall_feature TEXT,
  PRIMARY KEY (user_id, date)
);
CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_daily(date);

CREATE TABLE IF NOT EXISTS invoices (
  id          INTEGER PRIMARY KEY,
  account_id  INTEGER NOT NULL REFERENCES accounts(id),
  month       TEXT NOT NULL,
  plan        TEXT NOT NULL,
  seats       INTEGER NOT NULL,
  amount      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS offers (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  target_plan TEXT NOT NULL,
  discount    TEXT,
  headline    TEXT NOT NULL,
  details     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS templates (
  id          TEXT PRIMARY KEY,
  segment     TEXT NOT NULL,
  audience    TEXT NOT NULL,
  description TEXT,
  subject     TEXT NOT NULL,
  body        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  segment     TEXT NOT NULL,
  template_id TEXT,
  offer_id    TEXT,
  created_at  TEXT NOT NULL,
  source      TEXT NOT NULL DEFAULT 'live'   -- 'historical' (generated baseline) | 'live'
);

CREATE TABLE IF NOT EXISTS outreach_emails (
  id              INTEGER PRIMARY KEY,
  campaign_id     INTEGER REFERENCES campaigns(id),
  kind            TEXT NOT NULL DEFAULT 'outbound' CHECK (kind IN ('outbound','reply')),
  parent_id       INTEGER REFERENCES outreach_emails(id),
  inbound_id      INTEGER,                    -- inbound message this replies to
  account_id      INTEGER REFERENCES accounts(id),
  user_id         INTEGER REFERENCES users(id),
  to_email        TEXT NOT NULL,
  cc_emails       TEXT,
  subject         TEXT NOT NULL,
  body            TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending_approval'
                  CHECK (status IN ('draft','pending_approval','approved','sent','rejected','failed','blocked')),
  status_detail   TEXT,
  template_id     TEXT,
  offer_id        TEXT,
  model           TEXT,
  llm_seconds     REAL,
  rationale       TEXT,
  facts_json      TEXT,
  message_id      TEXT,
  in_reply_to     TEXT,
  references_hdr  TEXT,
  tracking_token  TEXT UNIQUE,
  created_at      TEXT NOT NULL,
  approved_by     TEXT,
  approved_at     TEXT,
  sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_status ON outreach_emails(status);
CREATE INDEX IF NOT EXISTS idx_emails_msgid ON outreach_emails(message_id);

CREATE TABLE IF NOT EXISTS email_events (
  id        INTEGER PRIMARY KEY,
  email_id  INTEGER NOT NULL REFERENCES outreach_emails(id),
  type      TEXT NOT NULL CHECK (type IN ('open','click','reply','unsubscribe','convert')),
  ts        TEXT NOT NULL,
  meta      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_email ON email_events(email_id, type);

CREATE TABLE IF NOT EXISTS inbound_messages (
  id               INTEGER PRIMARY KEY,
  message_id       TEXT UNIQUE,
  imap_uid         TEXT,
  from_email       TEXT NOT NULL,
  from_name        TEXT,
  cc_emails        TEXT,
  subject          TEXT,
  body             TEXT,
  in_reply_to      TEXT,
  references_hdr   TEXT,
  matched_email_id INTEGER REFERENCES outreach_emails(id),
  intent           TEXT,
  sentiment        TEXT,
  summary          TEXT,
  received_at      TEXT NOT NULL,
  processed_at     TEXT
);

CREATE TABLE IF NOT EXISTS conversions (
  id          INTEGER PRIMARY KEY,
  account_id  INTEGER NOT NULL REFERENCES accounts(id),
  email_id    INTEGER REFERENCES outreach_emails(id),
  plan_from   TEXT,
  plan_to     TEXT,
  mrr_delta   REAL NOT NULL DEFAULT 0,
  ts          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression (
  email   TEXT PRIMARY KEY,
  reason  TEXT,
  ts      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id     INTEGER PRIMARY KEY,
  ts     TEXT NOT NULL,
  actor  TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT
);

-- Agent observability: every agent run (chat, writer, inbox, analytics, openclaw) and its steps.
CREATE TABLE IF NOT EXISTS agent_runs (
  id          INTEGER PRIMARY KEY,
  agent       TEXT NOT NULL,
  session_id  TEXT,
  parent_id   INTEGER,
  input       TEXT,
  output      TEXT,
  status      TEXT NOT NULL DEFAULT 'running',   -- running | ok | error
  model       TEXT,
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  ms          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON agent_runs(session_id);

CREATE TABLE IF NOT EXISTS agent_steps (
  id        INTEGER PRIMARY KEY,
  run_id    INTEGER NOT NULL REFERENCES agent_runs(id),
  seq       INTEGER NOT NULL,
  kind      TEXT NOT NULL,          -- llm | tool | guard | escalation | info | error
  name      TEXT,
  input     TEXT,
  output    TEXT,
  status    TEXT DEFAULT 'ok',
  ms        INTEGER,
  ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id, seq);

CREATE TABLE IF NOT EXISTS escalations (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,        -- agent that escalated
  kind        TEXT NOT NULL,        -- approval_needed | hot_lead | pricing | objection | blocked_send | guard_fallback | deal_won | agent_request
  severity    TEXT NOT NULL DEFAULT 'medium',   -- low | medium | high
  title       TEXT NOT NULL,
  detail      TEXT,
  email_id    INTEGER,
  account_id  INTEGER,
  run_id      INTEGER,
  status      TEXT NOT NULL DEFAULT 'open',     -- open | resolved
  dedupe_key  TEXT UNIQUE,
  created_at  TEXT NOT NULL,
  resolved_by TEXT,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL,
  role        TEXT NOT NULL,        -- user | assistant
  content     TEXT NOT NULL,
  mode        TEXT,
  run_id      INTEGER,
  artifacts   TEXT,                 -- JSON list: charts, tables, drafts
  ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id);
