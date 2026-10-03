"""Prebuilt email templates + offers.

Templates use string.Template ${placeholders}. Numbers come from scoring (deterministic facts);
the LLM only fills ${opening} and ${value_para} (and may propose a subject). {{OFFER_LINK}} is replaced at send
time by a tracked link, {{UNSUB_LINK}} by the unsubscribe link.
"""
import string

OFFERS = {
    "enterprise_trial": dict(name="Enterprise 30-day trial", target_plan="enterprise", discount="30-day free trial",
                             headline="Try FlowDesk Enterprise free for 30 days",
                             details="SSO/SAML, audit log, advanced permissions, unlimited seats with true-up, "
                                     "99.9% SLA and a named success manager. First 30 days free; 15% off an annual plan."),
    "pro_20": dict(name="Pro 20% off", target_plan="pro", discount="20% off first year",
                   headline="Unlock FlowDesk Pro - 20% off your first year",
                   details="Unlimited exports, automations and history, plus API access."),
    "winback_2mo": dict(name="Win-back: 2 months on us", target_plan="same", discount="2 months free",
                        headline="Two months of FlowDesk on us",
                        details="We'll credit two months and give you a free workflow review with a solutions engineer."),
    "reactivation_guide": dict(name="Reactivation: guided setup", target_plan="pro", discount="free 1:1 setup",
                               headline="A free 20-minute guided setup",
                               details="We'll help you import your data and build your first automation."),
}

T = {}

T["champion_intro"] = dict(
    segment="expansion", audience="champion", offer="enterprise_trial",
    description="Ask the account's power user (champion) to introduce us to their VP for an Enterprise conversation.",
    subject="${first_name}, your team's FlowDesk usage (and a quick favor)",
    body="""Hi ${first_name},

${opening}

A few numbers from ${account_name}'s workspace over the last 90 days:
  - ${active_users} people actively using FlowDesk on ${seats} purchased seats (${seat_util}% utilization)
  - ${power_users} daily power users, and you're one of the most active
  - usage is up ${growth_pct}% versus the previous quarter
  - ${paywall_hits} times someone hit a Team-plan limit (${top_locked})

${value_para}

Would you be open to introducing me to ${vp_name} (${vp_title})? I'd share a short usage report showing how teams like yours roll out Enterprise, and there's a 30-day free trial so nothing changes for your team in the meantime.

Details here: {{OFFER_LINK}}

Thanks for being such a great FlowDesk champion,
${sender_name}
${sender_title}

Unsubscribe: {{UNSUB_LINK}}
""")

T["vp_enterprise_pitch"] = dict(
    segment="expansion", audience="vp", offer="enterprise_trial",
    description="Direct note to the VP with usage stats and the Enterprise ROI.",
    subject="${account_name}: ${active_users} employees already rely on FlowDesk",
    body="""Hi ${first_name},

${opening}

FlowDesk has spread organically at ${account_name}:
  - ${active_users} employees used it in the last 30 days, on ${seats} purchased seats (${seat_util}% utilization)
  - ${power_users} of them use it daily; overall usage grew ${growth_pct}% quarter over quarter
  - your teams hit Team-plan limits ${paywall_hits} times in 90 days, most often on ${top_locked}

${value_para}

Overview and free 30-day trial: {{OFFER_LINK}}

Open to a 20-minute call next week?

${sender_name}
${sender_title}

Unsubscribe: {{UNSUB_LINK}}
""")

T["pro_upsell"] = dict(
    segment="pro_upsell", audience="champion", offer="pro_20",
    description="Free-plan power user who keeps hitting limits.",
    subject="${first_name}, you've outgrown the free plan",
    body="""Hi ${first_name},

${opening}

In the last 90 days you were active on ${active_days} days and ran into free-plan limits ${paywall_hits} times (mostly ${top_locked}).

${value_para}

Pro removes those limits, and we're offering 20% off your first year: {{OFFER_LINK}}

${sender_name}
${sender_title}

Unsubscribe: {{UNSUB_LINK}}
""")

T["winback"] = dict(
    segment="winback", audience="champion", offer="winback_2mo",
    description="Paid account whose usage dropped sharply.",
    subject="Checking in on FlowDesk at ${account_name}",
    body="""Hi ${first_name},

${opening}

We noticed activity at ${account_name} is down ${decline_pct}% compared with last quarter, and we'd like to make sure FlowDesk is still working for your team.

${value_para}

We'd like to give you two months on us and a free workflow review: {{OFFER_LINK}}

${sender_name}
${sender_title}

Unsubscribe: {{UNSUB_LINK}}
""")

T["reactivation"] = dict(
    segment="reactivation", audience="champion", offer="reactivation_guide",
    description="Signed up, then went quiet.",
    subject="${first_name}, want a hand getting set up?",
    body="""Hi ${first_name},

${opening}

${value_para}

If you'd like, we can do a free 20-minute guided setup: {{OFFER_LINK}}

${sender_name}
${sender_title}

Unsubscribe: {{UNSUB_LINK}}
""")

SEGMENT_TEMPLATE = {"expansion": "champion_intro", "pro_upsell": "pro_upsell", "winback": "winback",
                    "reactivation": "reactivation"}


def render(text, values):
    return string.Template(text).safe_substitute(values)


def unresolved(text):
    """Placeholders left after rendering (bug detector for tests)."""
    return [m.group(0) for m in string.Template.pattern.finditer(text) if m.group("named") or m.group("braced")]


def seed(conn):
    for oid, o in OFFERS.items():
        conn.execute("INSERT OR REPLACE INTO offers(id,name,target_plan,discount,headline,details) VALUES (?,?,?,?,?,?)",
                     (oid, o["name"], o["target_plan"], o["discount"], o["headline"], o["details"]))
    for tid, t in T.items():
        conn.execute("INSERT OR REPLACE INTO templates(id,segment,audience,description,subject,body) VALUES (?,?,?,?,?,?)",
                     (tid, t["segment"], t["audience"], t["description"], t["subject"], t["body"]))
