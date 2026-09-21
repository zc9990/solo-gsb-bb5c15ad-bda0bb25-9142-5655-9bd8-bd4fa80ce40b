"""Unit tests for the decision engine: graph, cycles, fixed point, conflicts."""

import pytest

from app import engine


def r(code, condition, decision=None, depends_on=None, priority=100, name=None):
    return engine.RuleSpec(
        code=code,
        name=name or code,
        condition=condition,
        decision=decision or {},
        depends_on=depends_on or [],
        priority=priority,
    )


# --------------------------------------------------------------------------- #
# Expression safety
# --------------------------------------------------------------------------- #


def test_rejects_function_calls_and_dunder():
    with pytest.raises(engine.RuleSetValidationError):
        engine.parse_rules([{"code": "A", "condition": "__import__('os')"}])
    with pytest.raises(engine.RuleSetValidationError):
        engine.parse_rules([{"code": "A", "condition": "doc.__class__"}])
    with pytest.raises(engine.RuleSetValidationError):
        engine.parse_rules([{"code": "A", "condition": "str(doc)"}])


def test_undefined_comparison_is_false():
    assert engine.evaluate([r("A", "doc.missing > 5", {"x": 1})], "priority", {})["hits"] == []


# --------------------------------------------------------------------------- #
# Cycles and references
# --------------------------------------------------------------------------- #


def test_cycle_is_rejected_with_path():
    rules = [
        r("A", "True", {"v": 1}, depends_on=["C"]),
        r("B", "True", {"v": 2}, depends_on=["A"]),
        r("C", "True", {"v": 3}, depends_on=["B"]),
    ]
    with pytest.raises(engine.RuleSetValidationError) as exc:
        engine.topological_order(rules)
    cycle = exc.value.cycle
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"A", "B", "C"}


def test_self_dependency_rejected():
    with pytest.raises(engine.RuleSetValidationError):
        engine.parse_rules([{"code": "A", "condition": "True", "depends_on": ["A"]}])


def test_unknown_dependency_rejected():
    with pytest.raises(engine.RuleSetValidationError) as exc:
        engine.parse_rules([{"code": "A", "condition": "True", "depends_on": ["X"]}])
    assert "unknown rule" in exc.value.errors[0]


def test_duplicate_code_rejected():
    payload = [
        {"code": "A", "condition": "True"},
        {"code": "A", "condition": "False"},
    ]
    with pytest.raises(engine.RuleSetValidationError, match="Duplicate"):
        engine.parse_rules(payload)


# --------------------------------------------------------------------------- #
# Dependency-driven fixed point
# --------------------------------------------------------------------------- #


def test_dependent_rule_fires_in_topological_order():
    rules = [
        r("DOWNSTREAM", 'derived.tier == "gold"', {"discount": 0.2}, depends_on=["TIER"]),
        r("TIER", 'doc.spend >= 1000', {"tier": "gold"}),
    ]
    result = engine.evaluate(rules, "priority", {"spend": 1500})
    assert result["stable"] is True
    assert result["evaluation_order"] == ["TIER", "DOWNSTREAM"]
    assert result["hit_order"] == ["TIER", "DOWNSTREAM"]
    assert result["outputs"] == {"tier": "gold", "discount": 0.2}


def test_rules_that_become_fireable_later_run_to_fixed_point():
    # C only becomes fireable after A and B have run; rounds must converge.
    rules = [
        r("C", 'derived.a == 1 and derived.b == 2', {"c": 3}, depends_on=["A", "B"]),
        r("A", "doc.x > 0", {"a": 1}),
        r("B", "derived.a == 1", {"b": 2}, depends_on=["A"]),
    ]
    result = engine.evaluate(rules, "collect_all", {"x": 1})
    assert result["stable"] is True
    assert result["hit_order"] == ["A", "B", "C"]
    assert result["outputs"] == {"a": 1, "b": 2, "c": 3}


def test_no_hits_when_document_does_not_match():
    result = engine.evaluate([r("A", "doc.age >= 18", {"adult": True})], "priority", {"age": 5})
    assert result["hits"] == []
    assert result["outputs"] == {}
    assert result["stable"] is True


# --------------------------------------------------------------------------- #
# Conflict policies
# --------------------------------------------------------------------------- #


def _conflicting_rules():
    return [
        r("LOW", "True", {"action": "approve", "risk": "low"}, priority=100),
        r("HIGH", "True", {"action": "deny", "risk": "high"}, priority=500),
    ]


def test_priority_policy_high_priority_wins():
    result = engine.evaluate(_conflicting_rules(), "priority", {})
    assert result["outputs"]["action"] == "deny"
    conflict = next(c for c in result["conflicts"] if c["output"] == "action")
    assert conflict["winner"]["rule"] == "HIGH"
    assert {l["rule"] for l in conflict["losers"]} == {"LOW"}
    assert "highest priority" in conflict["message"]


def test_collect_all_keeps_last_write():
    rules = _conflicting_rules()
    result = engine.evaluate(rules, "collect_all", {})
    # LOW appears first in evaluation order, HIGH second -> HIGH overwrites
    assert result["outputs"]["action"] == "deny"
    assert result["conflicts"][0]["policy"] == "collect_all"


def test_first_hit_keeps_first_rule():
    result = engine.evaluate(_conflicting_rules(), "first_hit", {})
    assert result["rounds"] == 1
    assert result["hit_order"] == ["LOW"]
    assert result["outputs"]["action"] == "approve"


def test_deny_overrides_regardless_of_priority():
    rules = [
        r("STRONG_DENY", "True", {"action": "deny"}, priority=999),
        r("LATE_APPROVE", "True", {"action": "approve"}, priority=1),
    ]
    result = engine.evaluate(rules, "deny_overrides", {})
    assert result["outputs"]["action"] == "deny"
    conflict = next(c for c in result["conflicts"] if c["output"] == "action")
    assert "deny" in conflict["message"]


def test_same_value_is_not_a_conflict():
    rules = [r("A", "True", {"action": "deny"}), r("B", "True", {"action": "deny"})]
    result = engine.evaluate(rules, "priority", {})
    assert result["conflicts"] == []


def test_expression_based_decision_value():
    rules = [r("A", "True", {"double": "= doc.n * 2"})]
    result = engine.evaluate(rules, "priority", {"n": 21})
    assert result["outputs"]["double"] == 42
