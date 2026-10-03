"""SQLite helpers."""
import datetime as dt
import json
import os
import sqlite3

from . import config

SCHEMA = os.path.join(config.ROOT, "schema.sql")


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def connect(cfg=None):
    cfg = cfg or config.load()
    os.makedirs(os.path.dirname(cfg["db_path"]), exist_ok=True)
    conn = sqlite3.connect(cfg["db_path"], timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")   # sandbox Landlock: no writable temp dir for SQLite spill files
    return conn


def init_schema(conn):
    with open(SCHEMA) as f:
        conn.executescript(f.read())


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def one(conn, sql, args=()):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None


def audit(conn, actor, action, detail=None):
    conn.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES (?,?,?,?)",
                 (now(), actor, action, json.dumps(detail) if not isinstance(detail, str) else detail))


def log_event(conn, email_id, type_, meta=None):
    conn.execute("INSERT INTO email_events(email_id, type, ts, meta) VALUES (?,?,?,?)",
                 (email_id, type_, now(), json.dumps(meta) if meta else None))
