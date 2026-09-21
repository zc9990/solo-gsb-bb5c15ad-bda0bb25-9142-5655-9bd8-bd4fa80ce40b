"""Idempotent demo data: a small loan-approval rule set.

Dependency chain::

    R_AGE       -> R_BASELINE (age decides baseline eligibility)
    R_SCORE     -> R_BASELINE (credit score feeds risk)
    R_BASELINE  -> R_HIGH_RISK / R_LOW_RISK / R_MANUAL_REVIEW
    R_BLACKLIST -> R_HIGH_RISK (blacklist forces deny)

Conflict policy is ``priority`` so the blacklist deny beats low-risk approve.
"""

from __future__ import annotations

from . import engine, store

SEED_NAME = "Demo: Loan Approval"

SEED_RULES = [
    {
        "code": "R_AGE",
        "name": "Adult applicant",
        "condition": "doc.age >= 18",
        "decision": {"adult": True},
        "priority": 100,
    },
    {
        "code": "R_UNDERAGE",
        "name": "Underage applicant",
        "condition": "doc.age < 18",
        "decision": {"adult": False, "action": "deny", "reason": "underage"},
        "priority": 900,
    },
    {
        "code": "R_SCORE",
        "name": "Credit score bands",
        "condition": "doc.credit_score is not None",
        "decision": {"score_band": '= "high" if doc.credit_score >= 750 else ("low" if doc.credit_score < 600 else "medium")'},
        "priority": 100,
    },
    {
        "code": "R_BASELINE",
        "name": "Baseline eligibility",
        "condition": "derived.adult == True and derived.score_band is not None",
        "depends_on": ["R_AGE", "R_SCORE"],
        "decision": {"eligible": '= derived.score_band != "low"'},
        "priority": 100,
    },
    {
        "code": "R_BLACKLIST",
        "name": "Blacklisted customer",
        "condition": "doc.customer_id in doc['blacklist']",
        "decision": {"risk": "high", "action": "deny", "reason": "blacklisted"},
        "priority": 800,
    },
    {
        "code": "R_HIGH_RISK",
        "name": "High risk review",
        "condition": 'derived.eligible == False or derived.risk == "high"',
        "depends_on": ["R_BASELINE", "R_BLACKLIST"],
        "decision": {"risk": "high", "action": "deny", "reason": "high_risk"},
        "priority": 500,
    },
    {
        "code": "R_MANUAL_REVIEW",
        "name": "Manual review",
        "condition": 'derived.eligible == True and derived.score_band == "medium"',
        "depends_on": ["R_BASELINE"],
        "decision": {"risk": "medium", "action": "review", "reason": "medium_score"},
        "priority": 300,
    },
    {
        "code": "R_LOW_RISK",
        "name": "Automatic approval",
        "condition": 'derived.eligible == True and derived.score_band == "high"',
        "depends_on": ["R_BASELINE"],
        "decision": {"risk": "low", "action": "approve", "reason": "strong_score"},
        "priority": 200,
    },
]

SEED_POLICY = "priority"


def seed_if_empty(conn) -> dict:
    existing = [r for r in store.list_rulesets(conn) if r["name"] == SEED_NAME]
    if existing:
        return {"created": False, "ruleset_id": existing[0]["id"], "message": "Seed already present"}
    rs = store.create_ruleset(
        conn,
        name=SEED_NAME,
        description="Demo loan approval rule set with dependencies and priority conflicts",
        conflict_policy=SEED_POLICY,
        rules=[r.to_dict() for r in engine.parse_rules(SEED_RULES)],
        note="seeded demo data",
    )
    return {"created": True, "ruleset_id": rs["id"], "message": "Demo rule set created"}
