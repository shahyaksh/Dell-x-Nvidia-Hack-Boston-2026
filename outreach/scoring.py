"""Deterministic usage analytics: power users, account expansion score, segments.

The numbers produced here are the ONLY numbers that appear in outreach emails; the LLM never invents stats.
"""
import datetime as dt
from collections import Counter

from . import db

SEGMENTS = ("expansion", "pro_upsell", "winback", "reactivation", "nurture", "enterprise_healthy")


def _as_of(conn):
    return dt.date.fromisoformat(conn.execute("SELECT MAX(date) FROM usage_daily").fetchone()[0])


def user_stats(conn, as_of=None):
    as_of = as_of or _as_of(conn)
    d30, d90, d180 = (as_of - dt.timedelta(days=n) for n in (30, 90, 180))
    q = """
    SELECT u.id user_id, u.account_id, u.name, u.email, u.title,
      SUM(CASE WHEN date > :d90 THEN 1 ELSE 0 END)                       active_days_90,
      SUM(CASE WHEN date > :d30 THEN 1 ELSE 0 END)                       active_days_30,
      SUM(CASE WHEN date > :d90 THEN minutes ELSE 0 END)                 minutes_90,
      SUM(CASE WHEN date > :d180 AND date <= :d90 THEN minutes ELSE 0 END) minutes_prev90,
      SUM(CASE WHEN date > :d90 THEN paywall_hits ELSE 0 END)            paywall_90,
      SUM(CASE WHEN date > :d90 AND reports > 0 THEN 1 ELSE 0 END)       f_reports,
      SUM(CASE WHEN date > :d90 AND api_calls > 0 THEN 1 ELSE 0 END)     f_api,
      SUM(CASE WHEN date > :d90 AND automations > 0 THEN 1 ELSE 0 END)   f_auto,
      SUM(CASE WHEN date > :d90 AND collab_invites > 0 THEN 1 ELSE 0 END) f_collab,
      MAX(date) last_active
    FROM users u LEFT JOIN usage_daily d ON d.user_id = u.id
    GROUP BY u.id"""
    out = db.rows(conn, q, dict(d30=d30.isoformat(), d90=d90.isoformat(), d180=d180.isoformat()))
    max_days = max((r["active_days_90"] or 0) for r in out) or 1
    max_min = max((r["minutes_90"] or 0) for r in out) or 1
    for r in out:
        for k in ("active_days_90", "active_days_30", "minutes_90", "minutes_prev90", "paywall_90"):
            r[k] = r[k] or 0
        breadth = sum(1 for k in ("f_reports", "f_api", "f_auto", "f_collab") if (r[k] or 0) >= 3)
        r["feature_breadth"] = breadth
        r["power_score"] = round(100 * (0.40 * r["active_days_90"] / max_days + 0.25 * r["minutes_90"] / max_min
                                        + 0.15 * breadth / 4 + 0.20 * min(r["paywall_90"] / 40, 1)), 1)
        r["is_power_user"] = r["active_days_90"] >= 40 and r["power_score"] >= 45
    return out


def account_stats(conn, as_of=None):
    as_of = as_of or _as_of(conn)
    users = user_stats(conn, as_of)
    by_acct = {}
    for u in users:
        by_acct.setdefault(u["account_id"], []).append(u)
    d90 = (as_of - dt.timedelta(days=90)).isoformat()
    locked = {}
    for r in conn.execute("""SELECT u.account_id, d.paywall_feature f, SUM(d.paywall_hits) n FROM usage_daily d
                             JOIN users u ON u.id = d.user_id WHERE d.date > ? AND d.paywall_feature IS NOT NULL
                             GROUP BY 1, 2""", (d90,)):
        locked.setdefault(r["account_id"], Counter())[r["f"]] += r["n"]
    out = []
    for a in db.rows(conn, "SELECT * FROM accounts ORDER BY id"):
        us = by_acct.get(a["id"], [])
        active30 = sum(1 for u in us if u["active_days_30"] > 0)
        m90 = sum(u["minutes_90"] for u in us)
        mprev = sum(u["minutes_prev90"] for u in us)
        growth = (m90 - mprev) / mprev if mprev else (1.0 if m90 else 0.0)
        power = [u for u in us if u["is_power_user"]]
        champion = max(us, key=lambda u: u["power_score"]) if us else None
        top_locked = [f for f, _ in locked.get(a["id"], Counter()).most_common(3)]
        s = dict(a)
        s.update(users_total=len(us), active_users_30=active30,
                 seat_util=round(100 * active30 / max(a["seats_purchased"], 1)),
                 power_users=len(power), minutes_90=m90, minutes_prev90=mprev, growth=round(growth, 3),
                 paywall_90=sum(u["paywall_90"] for u in us), top_locked=top_locked,
                 champion=champion, last_active=max((u["last_active"] or "") for u in us) if us else None)
        s["segment"] = segment(s)
        s["score"] = score(s)
        out.append(s)
    return out


def segment(s):
    if s["plan"] == "enterprise":
        return "enterprise_healthy"
    if s["active_users_30"] == 0:
        return "reactivation"
    if s["plan"] != "free" and s["growth"] <= -0.35:
        return "winback"
    if s["plan"] in ("team", "pro") and s["growth"] >= 0.2 and (s["seat_util"] >= 90 or s["power_users"] >= 3):
        return "expansion"
    if s["plan"] == "free" and s["paywall_90"] >= 20:
        return "pro_upsell"
    return "nurture"


def score(s):
    seg = s["segment"]
    if seg == "expansion":
        return round(min(s["seat_util"], 150) * 0.4 + min(s["power_users"], 15) * 2
                     + min(max(s["growth"], 0), 1.5) * 20 + min(s["paywall_90"] / 50, 1) * 10, 1)
    if seg == "pro_upsell":
        return round(min(s["paywall_90"], 200) / 2 + (s["champion"] or {}).get("power_score", 0) / 2, 1)
    if seg == "winback":
        return round(-s["growth"] * 100 * (1 + s["mrr"] / 1000), 1)
    if seg == "reactivation":
        return round(10 + s["users_total"], 1)
    return 0.0


def targets(conn, seg, top=10):
    accts = [a for a in account_stats(conn) if a["segment"] == seg]
    return sorted(accts, key=lambda a: -a["score"])[:top]


def fmt_feature(f):
    return f.replace("_", " ").replace("sso", "SSO").replace("api", "API")


def facts(a, audience="champion"):
    """Template values for one account (+ who we are writing to)."""
    ch = a["champion"] or {}
    if audience == "vp":
        name, email, user_id = a["vp_name"] or "there", a["vp_email"], None
    else:
        name, email, user_id = ch.get("name", "there"), ch.get("email"), ch.get("user_id")
    return {
        "account_id": a["id"], "account_name": a["name"], "plan": a["plan"], "industry": a["industry"],
        "user_id": user_id, "to_email": email, "first_name": name.split()[0],
        "recipient_name": name, "recipient_title": ch.get("title") if audience != "vp" else a["vp_title"],
        "vp_name": a["vp_name"], "vp_title": a["vp_title"], "vp_email": a["vp_email"],
        "champion_name": ch.get("name"), "champion_title": ch.get("title"),
        "active_users": a["active_users_30"], "users_total": a["users_total"], "seats": a["seats_purchased"],
        "seat_util": a["seat_util"], "power_users": a["power_users"],
        "growth_pct": round(a["growth"] * 100), "decline_pct": round(-a["growth"] * 100),
        "paywall_hits": a["paywall_90"], "top_locked": ", ".join(fmt_feature(f) for f in a["top_locked"]) or "plan limits",
        "active_days": ch.get("active_days_90", 0), "champion_power_score": ch.get("power_score", 0),
        "mrr": a["mrr"], "segment": a["segment"], "score": a["score"],
    }


def summary_line(a):
    return (f"{a['name']} [{a['plan']}] score={a['score']}: {a['active_users_30']}/{a['seats_purchased']} seats active "
            f"({a['seat_util']}%), {a['power_users']} power users, growth {round(a['growth'] * 100):+d}%, "
            f"{a['paywall_90']} paywall hits ({', '.join(a['top_locked']) or '-'}); champion "
            f"{(a['champion'] or {}).get('name')} ({(a['champion'] or {}).get('title')})")
