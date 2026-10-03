#!/usr/bin/env python3
"""Generate a deterministic, believable 12-month usage dataset for a B2B SaaS product (FlowDesk).

Archetypes are planted on purpose so the agents have real signal to find:
  viral_team      Team plan, usage growing, more active users than seats  -> Enterprise expansion
  free_power      Free plan, heavy daily use, hits the paywall constantly  -> Pro upsell
  declining       Paid plan, usage falling hard over the last ~4 months    -> win-back
  dormant         Signed up, used it briefly, went quiet                   -> reactivation
  steady_pro / steady_team / enterprise   healthy baseline / already Enterprise

Also generates 12 months of HISTORICAL campaigns + email events + conversions so the funnel has a baseline.

  python3 data/generate_data.py [--seed 42] [--accounts 60] [--end 2026-10-03]
All synthetic addresses use the reserved .example TLD, so nothing can ever be delivered to them.
"""
import argparse
import datetime as dt
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from outreach import config, db, templates  # noqa: E402

FIRST = "Ava Liam Noah Emma Olivia Mason Sophia Lucas Mia Ethan Isabella Aiden Amelia Logan Harper Elijah Evelyn James " \
        "Abigail Benjamin Emily Jacob Ella Michael Avery Daniel Sofia Henry Camila Jackson Aria Sebastian Scarlett Jack " \
        "Priya Arjun Wei Mei Kenji Yuki Omar Fatima Diego Lucia Mateo Valentina Ravi Ananya Chen Hana".split()
LAST = "Smith Johnson Lee Garcia Martinez Brown Davis Lopez Wilson Anderson Thomas Taylor Moore Jackson Martin Thompson " \
       "White Harris Clark Lewis Robinson Walker Young Allen King Wright Scott Patel Shah Kumar Nguyen Kim Chen Singh " \
       "Rossi Muller Silva Tanaka Okafor Haddad".split()
CO_A = "Acme Northwind Bluefin Crestline Helix Lumen Orbit Pinnacle Quarry Redwood Summit Vertex Atlas Beacon Cobalt " \
       "Driftwood Ember Foundry Granite Harbor Ionic Juniper Keystone Lattice Meridian Nimbus Oakridge Prism Quantum " \
       "Riverstone Sable Tidal Umbra Vantage Willow Xenon Yonder Zephyr Aurora Basalt Cinder Delta Echo Fjord Glacier".split()
CO_B = "Robotics Logistics Health Analytics Labs Foods Energy Capital Media Systems Bio Retail Freight Studios Networks " \
       "Dynamics Partners Works Cloud Security".split()
INDUSTRIES = ["Manufacturing", "Logistics", "Healthcare", "Fintech", "Retail", "Media", "SaaS", "Energy", "Biotech"]
TITLES_IC = ["Data Analyst", "Operations Manager", "Product Manager", "Software Engineer", "Marketing Manager",
             "Financial Analyst", "Project Manager", "BizOps Lead", "Customer Success Manager", "Designer"]
VP_TITLES = ["VP of Operations", "VP of Engineering", "VP of Data", "COO", "VP of Finance", "Head of Product"]
PRICE = {"free": 0, "pro": 15, "team": 30, "enterprise": 55}          # $/seat/month
PAYWALL_FEATURES = {"free": ["export_limit", "automation_limit", "history_limit", "api_access"],
                    "pro": ["shared_workspaces", "seat_limit", "admin_console"],
                    "team": ["sso", "audit_log", "advanced_permissions", "seat_limit", "api_rate_limit"],
                    "enterprise": []}

CITIES = [  # (city, country, region, weight)
    ("San Francisco", "United States", "NA", 6), ("New York", "United States", "NA", 6), ("Austin", "United States", "NA", 4),
    ("Boston", "United States", "NA", 3), ("Seattle", "United States", "NA", 3), ("Chicago", "United States", "NA", 2),
    ("Toronto", "Canada", "NA", 3), ("Vancouver", "Canada", "NA", 1), ("London", "United Kingdom", "EMEA", 5),
    ("Manchester", "United Kingdom", "EMEA", 1), ("Berlin", "Germany", "EMEA", 4), ("Munich", "Germany", "EMEA", 2),
    ("Paris", "France", "EMEA", 2), ("Amsterdam", "Netherlands", "EMEA", 2), ("Dublin", "Ireland", "EMEA", 1),
    ("Stockholm", "Sweden", "EMEA", 1), ("Bangalore", "India", "APAC", 4), ("Mumbai", "India", "APAC", 2),
    ("Singapore", "Singapore", "APAC", 2), ("Sydney", "Australia", "APAC", 2), ("Tokyo", "Japan", "APAC", 2),
    ("Sao Paulo", "Brazil", "LATAM", 2), ("Mexico City", "Mexico", "LATAM", 1),
]

ARCHETYPES = [  # (name, count, plan, users range, seats factor, base activity, trend fn)
    ("viral_team", 7, "team"), ("free_power", 10, "free"), ("declining", 8, None), ("dormant", 9, "free"),
    ("steady_pro", 10, "pro"), ("steady_team", 8, "team"), ("enterprise", 8, "enterprise"),
]


def trend(arch, frac):
    """Activity multiplier at fraction `frac` (0=a year ago, 1=today) of the window."""
    if arch == "viral_team":
        return 0.35 + 1.3 * frac ** 1.6
    if arch == "declining":
        return 1.0 if frac < 0.62 else max(0.06, 1.0 - (frac - 0.62) / 0.38 * 0.95)
    if arch == "dormant":
        return 1.0 if frac < 0.3 else 0.0
    if arch == "free_power":
        return 0.6 + 0.5 * frac
    return 0.95 + 0.1 * math.sin(frac * 6.28)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--end", default=dt.date.today().isoformat())
    ap.add_argument("--fresh", action="store_true", help="delete the existing database first")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    rgeo = random.Random(a.seed + 1)
    city_w = [c[3] for c in CITIES]
    cfg = config.load()
    if a.fresh and os.path.exists(cfg["db_path"]):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cfg["db_path"] + suffix):
                os.remove(cfg["db_path"] + suffix)
    conn = db.connect(cfg)
    db.init_schema(conn)
    if conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
        sys.exit("database already has data; rerun with --fresh")

    end = dt.date.fromisoformat(a.end)
    start = end - dt.timedelta(days=364)
    days = [start + dt.timedelta(days=i) for i in range(365)]
    templates.seed(conn)

    names_used, co_used = set(), set()

    def person():
        while True:
            n = f"{rnd.choice(FIRST)} {rnd.choice(LAST)}"
            if n not in names_used:
                names_used.add(n)
                return n

    acct_id = user_id = 0
    usage_rows, user_rows, acct_rows, inv_rows = [], [], [], []
    acct_users = {}
    acct_geo = {}
    for arch, count, plan in ARCHETYPES:
        for _ in range(count):
            while True:
                co = f"{rnd.choice(CO_A)} {rnd.choice(CO_B)}"
                if co not in co_used:
                    co_used.add(co)
                    break
            acct_id += 1
            p = plan or rnd.choice(["pro", "team", "team"])
            n_users = {"viral_team": rnd.randint(26, 44), "free_power": rnd.randint(1, 3),
                       "declining": rnd.randint(6, 18), "dormant": rnd.randint(1, 3),
                       "steady_pro": rnd.randint(3, 9), "steady_team": rnd.randint(10, 22),
                       "enterprise": rnd.randint(40, 70)}[arch]
            seats = {"viral_team": int(n_users * rnd.uniform(0.72, 0.85)), "free_power": n_users,
                     "dormant": n_users, "enterprise": int(n_users * 1.15)}.get(arch, n_users + rnd.randint(0, 3))
            employees = max(n_users * rnd.randint(4, 15), 10) if p != "free" else rnd.randint(1, 40)
            domain = co.lower().replace(" ", "") + ".example"
            signup = start - dt.timedelta(days=rnd.randint(30, 700)) if arch != "dormant" \
                else start + dt.timedelta(days=rnd.randint(0, 60))
            vp = person()
            hq = rgeo.choices(CITIES, weights=city_w)[0]
            acct_geo[acct_id] = hq
            acct_rows.append((acct_id, co, domain, rnd.choice(INDUSTRIES), employees, p, seats,
                              PRICE[p] * seats, signup.isoformat(), vp, rnd.choice(VP_TITLES),
                              f"{vp.split()[0].lower()}.{vp.split()[1].lower()}@{domain}", arch,
                              hq[1], hq[0], hq[2]))
            for m in range(12):
                month = (start.replace(day=1) + dt.timedelta(days=31 * m)).replace(day=1)
                inv_rows.append((acct_id, month.strftime("%Y-%m"), p, seats, PRICE[p] * seats))
            ids = []
            for k in range(n_users):
                user_id += 1
                nm = person()
                # users of growing accounts join over time; others mostly existed from the start
                join_frac = rnd.uniform(0, 0.85) if arch == "viral_team" and k > n_users * 0.35 else rnd.uniform(0, 0.15)
                join = max(days[int(join_frac * 364)], signup)
                intensity = rnd.lognormvariate(0, 0.45) * (1.9 if k == 0 else 1.0)   # first user = champion-ish
                power = arch in ("viral_team", "free_power") and (k == 0 or rnd.random() < 0.3)
                user_rows.append((user_id, acct_id, nm, f"{nm.split()[0].lower()}.{nm.split()[1].lower()}@{domain}",
                                  rnd.choice(TITLES_IC), join.isoformat()))
                ids.append(user_id)
                last_seen = None
                for i, d in enumerate(days):
                    if d < join:
                        continue
                    f = trend(arch, i / 364)
                    base = 0.62 if power else 0.38
                    p_active = min(0.97, base * f * (1.0 if d.weekday() < 5 else 0.18) * min(intensity, 1.8))
                    if rnd.random() > p_active:
                        continue
                    last_seen = d
                    minutes = max(3, int(rnd.gauss(38, 14) * intensity * (1.6 if power else 1.0) * max(f, 0.3)))
                    sessions = max(1, minutes // rnd.randint(12, 25))
                    feats = PAYWALL_FEATURES[p]
                    hit_p = {"free_power": 0.75, "viral_team": 0.10 * f}.get(arch, 0.03)
                    hits = rnd.randint(1, 4) if feats and rnd.random() < hit_p else 0
                    usage_rows.append((
                        user_id, d.isoformat(), sessions, minutes, minutes * rnd.randint(2, 5),
                        rnd.randint(0, 3) if rnd.random() < 0.5 else 0,
                        rnd.randint(5, 120) if power and rnd.random() < 0.6 else 0,
                        rnd.randint(0, 4) if rnd.random() < (0.5 if power else 0.15) else 0,
                        1 if rnd.random() < (0.12 if arch == "viral_team" else 0.02) else 0,
                        hits, rnd.choice(feats) if hits else None))
                loc = hq if rgeo.random() < 0.8 else rgeo.choices(CITIES, weights=city_w)[0]   # some remote staff
                user_rows[-1] = user_rows[-1] + (last_seen.isoformat() if last_seen else None, loc[1], loc[0], loc[2])
            acct_users[acct_id] = ids

    conn.executemany("INSERT INTO accounts(id,name,domain,industry,employees,plan,seats_purchased,mrr,signup_date,"
                     "vp_name,vp_title,vp_email,archetype,country,city,region) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", acct_rows)
    conn.executemany("INSERT INTO users(id,account_id,name,email,title,created_at,last_seen,country,city,region) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     user_rows)
    conn.executemany("INSERT INTO usage_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)", usage_rows)
    conn.executemany("INSERT INTO invoices(account_id,month,plan,seats,amount) VALUES (?,?,?,?,?)", inv_rows)
    historical_campaigns(conn, rnd, start, end, acct_rows, acct_users)
    conn.commit()
    print(f"accounts={len(acct_rows)} users={len(user_rows)} usage_rows={len(usage_rows)} "
          f"window={start}..{end} db={cfg['db_path']}")


def historical_campaigns(conn, rnd, start, end, acct_rows, acct_users):
    """Baseline: last year's (generic, non-personalized) campaigns with typical funnel rates."""
    rates = {  # open, click|open, reply, convert   -- generic blasts perform modestly
        "expansion": (0.42, 0.22, 0.05, 0.03), "pro_upsell": (0.38, 0.18, 0.02, 0.04),
        "winback": (0.30, 0.10, 0.03, 0.01), "reactivation": (0.22, 0.08, 0.01, 0.01)}
    by_arch = {"expansion": ["viral_team", "steady_team"], "pro_upsell": ["free_power", "steady_pro"],
               "winback": ["declining"], "reactivation": ["dormant"]}
    tpl = {"expansion": "vp_enterprise_pitch", "pro_upsell": "pro_upsell", "winback": "winback",
           "reactivation": "reactivation"}
    email_id = 0
    for m in range(11):
        when = start + dt.timedelta(days=15 + 30 * m)
        seg = ["expansion", "pro_upsell", "winback", "reactivation"][m % 4]
        cur = conn.execute("INSERT INTO campaigns(name,segment,template_id,created_at,source) VALUES (?,?,?,?,?)",
                           (f"{when:%b %Y} {seg.replace('_', ' ')} blast", seg, tpl[seg], when.isoformat(),
                            "historical"))
        cid = cur.lastrowid
        targets = [r for r in acct_rows if r[12] in by_arch[seg]]
        for acc in targets:
            for uid in rnd.sample(acct_users[acc[0]], min(3, len(acct_users[acc[0]]))):
                email_id += 1
                ts = dt.datetime.combine(when, dt.time(9)) + dt.timedelta(minutes=rnd.randint(0, 600))
                cur = conn.execute(
                    "INSERT INTO outreach_emails(campaign_id,account_id,user_id,to_email,subject,body,status,template_id,"
                    "model,tracking_token,created_at,sent_at,approved_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, acc[0], uid, f"user{uid}@{acc[2]}", f"[{seg}] FlowDesk update", "(historical blast)",
                     "sent", tpl[seg], "generic-template", f"hist-{email_id}", ts.isoformat(), ts.isoformat(),
                     "marketing-automation"))
                eid = cur.lastrowid
                o, c, r, v = rates[seg]
                ev = []
                if rnd.random() < o:
                    ev.append(("open", ts + dt.timedelta(hours=rnd.randint(1, 30))))
                    if rnd.random() < c:
                        ev.append(("click", ts + dt.timedelta(hours=rnd.randint(2, 40))))
                if rnd.random() < r:
                    ev.append(("reply", ts + dt.timedelta(hours=rnd.randint(3, 72))))
                if rnd.random() < v:
                    ev.append(("convert", ts + dt.timedelta(days=rnd.randint(1, 14))))
                    conn.execute("INSERT INTO conversions(account_id,email_id,plan_from,plan_to,mrr_delta,ts) "
                                 "VALUES (?,?,?,?,?,?)", (acc[0], eid, acc[5], "upgrade", rnd.choice([150, 300, 600]),
                                                          (ts + dt.timedelta(days=3)).isoformat()))
                for t, when_ev in ev:
                    conn.execute("INSERT INTO email_events(email_id,type,ts) VALUES (?,?,?)",
                                 (eid, t, when_ev.isoformat(timespec="seconds")))


if __name__ == "__main__":
    main()
