"""Configuration: config.json in the project root (override with OUTREACH_CONFIG), env vars win."""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "product_name": "FlowDesk",
    "company_name": "FlowDesk Inc.",
    "sender_name": "Maya Patel",
    "sender_title": "Customer Success, FlowDesk",
    "sender_email": "",
    "gmail_config": "/sandbox/hand/gmail_config.json",   # {"email": ..., "app_password": ...}
    "mail_mode": "dryrun",                               # dryrun | gmail
    "db_path": os.path.join(ROOT, "data", "outreach.db"),
    "outbox_dir": os.path.join(ROOT, "data", "outbox"),
    "track_base_url": "http://127.0.0.1:8090",
    "allowlist": [],                                     # exact addresses that may receive real mail
    "allow_sender_plus": True,                           # sender+anything@domain is always allowed
    "demo_contacts": {"champion": "", "vp": ""},         # real inboxes that play the #1 expansion account
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8090,
    "dashboard_user": "admin",
    "dashboard_password": "outreach",
    "llm_base_url": "https://inference.local/v1",
    "llm_model": "route",                                # OpenShell route applies the gateway model anyway
    "llm_timeout": 300,
    "imap_lookback_days": 3,
}


def load():
    path = os.environ.get("OUTREACH_CONFIG", os.path.join(ROOT, "config.json"))
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    for key in ("mail_mode", "db_path", "outbox_dir", "track_base_url", "llm_base_url", "llm_model", "sender_email"):
        env = os.environ.get("OUTREACH_" + key.upper())
        if env:
            cfg[key] = env
    if not os.path.isabs(cfg["db_path"]):
        cfg["db_path"] = os.path.join(ROOT, cfg["db_path"])
    if not os.path.isabs(cfg["outbox_dir"]):
        cfg["outbox_dir"] = os.path.join(ROOT, cfg["outbox_dir"])
    return cfg


def gmail_credentials(cfg):
    with open(cfg["gmail_config"]) as f:
        creds = json.load(f)
    return creds["email"], creds["app_password"]


def sender_email(cfg):
    if cfg.get("sender_email"):
        return cfg["sender_email"]
    if cfg["mail_mode"] == "gmail" and os.path.exists(cfg["gmail_config"]):
        return gmail_credentials(cfg)[0]
    return "demo.sender@example.com"
