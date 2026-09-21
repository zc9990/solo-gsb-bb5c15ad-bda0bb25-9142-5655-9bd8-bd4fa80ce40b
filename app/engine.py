"""
Decision engine.

Responsibilities:
  * Validate rule conditions with a safe AST-based expression evaluator
    (no ``eval`` / no access to Python internals).
  * Build the dependency graph from ``depends_on``, reject unknown references
    and cyclic dependencies (returning the cycle path).
  * Evaluate rules in topological order and run to a fixed point so that
    rules consuming outputs of other rules always reach a stable result.
  * Resolve output conflicts according to the rule set's conflict policy and
    produce a human-readable explanation for every conflict.
"""

from __future__ import annotations

import ast
import operator
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class RuleSetValidationError(Exception):
    """Raised when a rule set cannot be saved (bad rules / cycle / ...)."""

    def __init__(self, errors: list[str], cycle: list[str] | None = None):
        super().__init__("; ".join(errors))
        self.errors = errors
        self.cycle = cycle or []


class ConditionError(Exception):
    """A rule condition could not be evaluated at runtime."""


# --------------------------------------------------------------------------- #
# Rule specification
# --------------------------------------------------------------------------- #


@dataclass
class RuleSpec:
    code: str
    name: str
    condition: str
    decision: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    priority: int = 100
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "condition": self.condition,
            "decision": self.decision,
            "depends_on": list(self.depends_on),
            "priority": self.priority,
            "description": self.description,
        }


CONFLICT_POLICIES = ("collect_all", "priority", "first_hit", "deny_overrides")


# --------------------------------------------------------------------------- #
# Safe expression evaluation
# --------------------------------------------------------------------------- #

# Only a strict whitelist of AST nodes is ever interpreted.
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.IfExp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.And,
    ast.Or,
    ast.Not,
    ast.USub,
    ast.UAdd,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}


class _Undefined:
    """Marker for a missing document/derived value."""

    _instance: "_Undefined | None" = None

    def __new__(cls) -> "_Undefined":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "<undefined>"

    def __bool__(self) -> bool:
        return False


UNDEFINED = _Undefined()


def is_undefined(value: Any) -> bool:
    return value is UNDEFINED


def _truthy(value: Any) -> bool:
    return value is not UNDEFINED and bool(value)


def validate_expression(source: str) -> None:
    """Static check: expression must parse and only use whitelisted nodes."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise RuleSetValidationError([f"Syntax error in expression {source!r}: {exc}"])

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleSetValidationError(
                [f"Disallowed expression element: {type(node).__name__} in {source!r}"]
            )
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise RuleSetValidationError([f"Disallowed name {node.id!r} in {source!r}"])
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise RuleSetValidationError(
                [f"Disallowed attribute {node.attr!r} in {source!r}"]
            )
        if isinstance(node, ast.Constant) and not isinstance(
            node.value, (int, float, str, bool, type(None))
        ):
            raise RuleSetValidationError(
                [f"Disallowed constant {node.value!r} in {source!r}"]
            )


def _eval(node: ast.AST, ctx: dict[str, dict[str, Any]]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, ctx)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        if node.id == "doc":
            return ctx["doc"]
        if node.id == "derived":
            return ctx["derived"]
        # Bare names resolve against the document first, then derived outputs.
        if node.id in ctx["doc"]:
            return ctx["doc"][node.id]
        return ctx["derived"].get(node.id, UNDEFINED)

    if isinstance(node, ast.Attribute):
        value = _eval(node.value, ctx)
        if isinstance(value, dict):
            return value.get(node.attr, UNDEFINED)
        raise ConditionError(f"Cannot read attribute {node.attr!r} of non-object")

    if isinstance(node, ast.Subscript):
        value = _eval(node.value, ctx)
        key = _eval(node.slice, ctx)
        if isinstance(value, dict):
            return value.get(key, UNDEFINED)
        if isinstance(value, (list, tuple, str)) and isinstance(key, int):
            try:
                return value[key]
            except IndexError:
                return UNDEFINED
        raise ConditionError(f"Cannot index {type(value).__name__} with {key!r}")

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _truthy(_eval(node.operand, ctx))
        operand = _eval(node.operand, ctx)
        if operand is UNDEFINED:
            return UNDEFINED
        if isinstance(node.op, ast.USub):
            return -operand
        return +operand

    if isinstance(node, ast.BinOp):
        left = _eval(node.left, ctx)
        right = _eval(node.right, ctx)
        if left is UNDEFINED or right is UNDEFINED:
            return UNDEFINED
        op = _BIN_OPS.get(type(node.op))
        if op is None:  # pragma: no cover - whitelisted above
            raise ConditionError(f"Unsupported operator {type(node.op).__name__}")
        return op(left, right)

    if isinstance(node, ast.BoolOp):
        want = isinstance(node.op, ast.And)
        result: Any = True if want else False
        for sub in node.values:
            sub_value = _eval(sub, ctx)
            if want:
                if not _truthy(sub_value):
                    return sub_value if sub_value is not UNDEFINED else False
                result = sub_value
            else:
                if _truthy(sub_value):
                    return sub_value
                result = sub_value if sub_value is not UNDEFINED else False
        return result

    if isinstance(node, ast.IfExp):
        return _eval(node.body if _truthy(_eval(node.test, ctx)) else node.orelse, ctx)

    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op_node, comparator_node in zip(node.ops, node.comparators):
            right = _eval(comparator_node, ctx)
            if not _compare(op_node, left, right):
                return False
            left = right
        return True

    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(el, ctx) for el in node.elts]

    if isinstance(node, ast.Dict):
        return {
            _eval(key, ctx): _eval(val, ctx)
            for key, val in zip(node.keys, node.values)
        }

    raise ConditionError(f"Unsupported expression node {type(node).__name__}")


def _compare(op_node: ast.AST, left: Any, right: Any) -> bool:
    # Missing data never raises: ordering comparisons against undefined are
    # False, equality is False and inequality is True (SQL-like NULL semantics).
    if isinstance(op_node, ast.Eq):
        return left is not UNDEFINED and right is not UNDEFINED and left == right
    if isinstance(op_node, ast.NotEq):
        if left is UNDEFINED or right is UNDEFINED:
            return not (left is UNDEFINED and right is UNDEFINED)
        return left != right
    if isinstance(op_node, (ast.Is, ast.IsNot)):
        result = left is right or (left is None and right is None)
        if isinstance(op_node, ast.Is):
            return result
        return not result
    if isinstance(op_node, ast.In):
        if left is UNDEFINED or not isinstance(right, (list, tuple, dict, str)):
            return False
        return left in right
    if isinstance(op_node, ast.NotIn):
        if left is UNDEFINED or not isinstance(right, (list, tuple, dict, str)):
            return True
        return left not in right

    if left is UNDEFINED or right is UNDEFINED:
        return False
    try:
        if isinstance(op_node, ast.Lt):
            return left < right
        if isinstance(op_node, ast.LtE):
            return left <= right
        if isinstance(op_node, ast.Gt):
            return left > right
        if isinstance(op_node, ast.GtE):
            return left >= right
    except TypeError:
        return False
    raise ConditionError(f"Unsupported comparison {type(op_node).__name__}")


def evaluate_condition(source: str, document: dict[str, Any], derived: dict[str, Any]) -> bool:
    tree = ast.parse(source, mode="eval")
    ctx = {"doc": document, "derived": derived}
    return _truthy(_eval(tree, ctx))


def evaluate_value_expression(source: str, document: dict[str, Any], derived: dict[str, Any]) -> Any:
    """Evaluate a decision value written as ``"= <expression>"``."""
    tree = ast.parse(source[1:].lstrip(), mode="eval")
    return _eval(tree, {"doc": document, "derived": derived})


# --------------------------------------------------------------------------- #
# Dependency graph
# --------------------------------------------------------------------------- #


def build_graph(rules: list[RuleSpec]) -> dict[str, list[str]]:
    codes = [r.code for r in rules]
    errors: list[str] = []

    seen: set[str] = set()
    for code in codes:
        if not code or not isinstance(code, str):
            errors.append("Rule code must be a non-empty string")
        elif code in seen:
            errors.append(f"Duplicate rule code: {code}")
        seen.add(code)

    graph: dict[str, list[str]] = {}
    for rule in rules:
        deps: list[str] = []
        for dep in rule.depends_on:
            if dep not in seen:
                errors.append(f"Rule {rule.code!r} depends on unknown rule {dep!r}")
            elif dep == rule.code:
                errors.append(f"Rule {rule.code!r} depends on itself")
            else:
                deps.append(dep)
        graph[rule.code] = deps

    if errors:
        raise RuleSetValidationError(errors)
    return graph


def detect_cycle(graph: dict[str, list[str]]) -> list[str]:
    """Return a cycle as an ordered list of node codes (empty if acyclic)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for neighbour in graph[node]:
            if color[neighbour] == GRAY:
                start = stack.index(neighbour)
                return stack[start:] + [neighbour]
            if color[neighbour] == WHITE:
                found = visit(neighbour)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return []


def topological_order(rules: list[RuleSpec]) -> list[RuleSpec]:
    """Kahn's algorithm; ties keep the authoring order for deterministic runs."""
    graph = build_graph(rules)
    cycle = detect_cycle(graph)
    if cycle:
        raise RuleSetValidationError(
            [f"Cyclic dependency detected: {' -> '.join(cycle)}"], cycle=cycle
        )

    by_code = {rule.code: rule for rule in rules}
    index_of = {rule.code: i for i, rule in enumerate(rules)}
    indegree = {code: 0 for code in graph}
    dependents: dict[str, list[str]] = defaultdict(list)
    for code, deps in graph.items():
        indegree[code] = len(deps)
        for dep in deps:
            dependents[dep].append(code)

    ready = deque(sorted((c for c, d in indegree.items() if d == 0), key=index_of.get))
    ordered: list[RuleSpec] = []
    while ready:
        code = ready.popleft()
        ordered.append(by_code[code])
        for dependent in dependents[code]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
        ready = deque(sorted(ready, key=index_of.get))  # stable tie-break
    return ordered


# --------------------------------------------------------------------------- #
# Validation of a full rule set
# --------------------------------------------------------------------------- #


def parse_rules(payload: list[dict[str, Any]]) -> list[RuleSpec]:
    errors: list[str] = []
    rules: list[RuleSpec] = []

    if not payload:
        raise RuleSetValidationError(["A rule set must contain at least one rule"])

    for i, raw in enumerate(payload):
        prefix = f"rules[{i}]"
        if not isinstance(raw, dict):
            errors.append(f"{prefix} must be an object")
            continue
        code = raw.get("code")
        if not isinstance(code, str) or not code.strip():
            errors.append(f"{prefix}.code must be a non-empty string")
        condition = raw.get("condition")
        if not isinstance(condition, str) or not condition.strip():
            errors.append(f"{prefix}.condition must be a non-empty string")
        else:
            try:
                validate_expression(condition)
            except RuleSetValidationError as exc:
                errors.extend(f"{prefix}({code}): {e}" for e in exc.errors)
        decision = raw.get("decision", {})
        if not isinstance(decision, dict):
            errors.append(f"{prefix}.decision must be an object")
        else:
            for key, value in decision.items():
                if isinstance(value, str) and value.startswith("="):
                    try:
                        validate_expression(value[1:].lstrip())
                    except RuleSetValidationError as exc:
                        errors.extend(
                            f"{prefix}({code}).decision.{key}: {e}" for e in exc.errors
                        )
        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            errors.append(f"{prefix}.depends_on must be a list of rule codes")
        priority = raw.get("priority", 100)
        if not isinstance(priority, int):
            errors.append(f"{prefix}.priority must be an integer")
        rules.append(
            RuleSpec(
                code=code or f"__invalid_{i}",
                name=raw.get("name", code or f"rule-{i}"),
                condition=condition or "False",
                decision=decision if isinstance(decision, dict) else {},
                depends_on=depends_on if isinstance(depends_on, list) else [],
                priority=priority if isinstance(priority, int) else 100,
                description=raw.get("description"),
            )
        )

    if errors:
        raise RuleSetValidationError(errors)

    graph = build_graph(rules)
    cycle = detect_cycle(graph)
    if cycle:
        raise RuleSetValidationError(
            [f"Cyclic dependency detected: {' -> '.join(cycle)}"], cycle=cycle
        )
    return rules


# --------------------------------------------------------------------------- #
# Fixed-point evaluation
# --------------------------------------------------------------------------- #


def _conflict_message(policy: str, key: str, winner: dict[str, Any], losers: list[dict[str, Any]]) -> str:
    loser_text = ", ".join(f"{l['rule']}={l['value']!r}" for l in losers)
    if policy == "priority":
        why = f"rule {winner['rule']!r} has the highest priority ({winner['priority']})"
    elif policy == "deny_overrides" and key == "action" and winner["value"] == "deny":
        why = "'deny' overrides every other action under the deny_overrides policy"
    elif policy == "first_hit":
        why = "first_hit keeps the first rule that wrote the key"
    else:
        why = "collect_all keeps the most recent write"
    return (
        f"Conflict on output {key!r}: {winner['rule']}={winner['value']!r} wins "
        f"over {loser_text} because {why}."
    )


def _incoming_wins(policy: str, key: str, current: dict[str, Any], incoming: dict[str, Any]) -> bool:
    """Decide whether an incoming conflicting write replaces the current one."""
    if policy == "collect_all":
        return True
    if policy == "priority":
        return incoming["priority"] > current["priority"]
    if policy == "first_hit":
        return False
    if policy == "deny_overrides":
        if key == "action":
            cur_deny, new_deny = current["value"] == "deny", incoming["value"] == "deny"
            if cur_deny and not new_deny:
                return False
            if new_deny and not cur_deny:
                return True
        return incoming["priority"] > current["priority"]
    raise ValueError(f"Unknown conflict policy: {policy}")


def evaluate(rules: list[RuleSpec], policy: str, document: dict[str, Any]) -> dict[str, Any]:
    if policy not in CONFLICT_POLICIES:
        raise RuleSetValidationError(
            [f"Unknown conflict_policy {policy!r}; allowed: {', '.join(CONFLICT_POLICIES)}"]
        )

    ordered = topological_order(rules)

    derived: dict[str, Any] = {}
    # effective[key] = {"value", "rule", "priority"}
    effective: dict[str, dict[str, Any]] = {}
    # attempts[key] = list of write attempts (applied or rejected)
    attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fired: list[dict[str, Any]] = []
    fired_codes: set[str] = set()
    warnings: list[dict[str, str]] = []

    def materialize_decision(rule: RuleSpec) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key, raw in rule.decision.items():
            if isinstance(raw, str) and raw.startswith("="):
                try:
                    values[key] = evaluate_value_expression(raw, document, derived)
                except Exception as exc:  # noqa: BLE001 - record and keep going
                    warnings.append(
                        {"rule": rule.code, "message": f"Decision value {key}: {exc}"}
                    )
                    values[key] = None
            else:
                values[key] = raw
        return values

    def fire(rule: RuleSpec, round_no: int) -> bool:
        outputs = materialize_decision(rule)
        changed = False
        for key, value in outputs.items():
            applied = True
            if key in effective and effective[key]["value"] != value:
                applied = _incoming_wins(policy, key, effective[key], {
                    "value": value, "rule": rule.code, "priority": rule.priority
                })
                if applied:
                    effective[key] = {"value": value, "rule": rule.code, "priority": rule.priority}
                    changed = True
            elif key not in effective:
                effective[key] = {"value": value, "rule": rule.code, "priority": rule.priority}
                changed = True
            attempts[key].append(
                {
                    "rule": rule.code,
                    "name": rule.name,
                    "priority": rule.priority,
                    "value": value,
                    "round": round_no,
                    "applied": applied,
                }
            )
        fired.append(
            {
                "order": len(fired) + 1,
                "code": rule.code,
                "name": rule.name,
                "round": round_no,
                "outputs": outputs,
            }
        )
        fired_codes.add(rule.code)
        # Conditions read derived outputs through the winning value map.
        derived.clear()
        derived.update({key: meta["value"] for key, meta in effective.items()})
        return changed

    stable = True
    rounds = 0

    if policy == "first_hit":
        rounds = 1
        for rule in ordered:
            try:
                hit = evaluate_condition(rule.condition, document, derived)
            except ConditionError as exc:
                warnings.append({"rule": rule.code, "message": str(exc)})
                hit = False
            if hit:
                fire(rule, rounds)
                break
    else:
        max_rounds = len(ordered) + 1
        for round_no in range(1, max_rounds + 1):
            rounds = round_no
            changed = False
            for rule in ordered:
                if rule.code in fired_codes:
                    continue
                try:
                    hit = evaluate_condition(rule.condition, document, derived)
                except ConditionError as exc:
                    warnings.append({"rule": rule.code, "message": str(exc)})
                    continue
                if hit:
                    changed = fire(rule, round_no) or changed
            if not changed:
                break
        else:
            # An acyclic graph always converges within N+1 rounds.
            stable = False

    # ----- build conflict explanations -------------------------------------
    conflicts: list[dict[str, Any]] = []
    for key, writes in attempts.items():
        distinct: list[dict[str, Any]] = []
        for write in writes:
            if not any(d["rule"] == write["rule"] and d["value"] == write["value"] for d in distinct):
                distinct.append(write)

        winner_value = effective[key]["value"]
        agreeing = [d for d in distinct if d["value"] == winner_value]
        losing = [d for d in distinct if d["value"] != winner_value]
        # No disagreement -> no conflict (everyone wrote the same value).
        if not losing:
            continue

        winner_rule = effective[key]["rule"]
        winner_entry = next(
            d for d in distinct if d["rule"] == winner_rule and d["value"] == winner_value
        )
        winner = {
            "rule": winner_entry["rule"],
            "name": winner_entry["name"],
            "value": winner_entry["value"],
            "priority": winner_entry["priority"],
        }
        losers = [
            {
                "rule": d["rule"],
                "name": d["name"],
                "value": d["value"],
                "priority": d["priority"],
            }
            for d in losing
        ]
        conflicts.append(
            {
                "output": key,
                "policy": policy,
                "winner": winner,
                "losers": losers,
                "agreements": [
                    {"rule": d["rule"], "value": d["value"], "priority": d["priority"]}
                    for d in agreeing
                    if d["rule"] != winner_rule
                ],
                "contenders": [
                    {"rule": d["rule"], "value": d["value"], "priority": d["priority"]}
                    for d in distinct
                ],
                "message": _conflict_message(policy, key, winner, losers),
            }
        )

    return {
        "stable": stable,
        "rounds": rounds,
        "evaluation_order": [r.code for r in ordered],
        "hit_order": [f["code"] for f in fired],
        "hits": fired,
        "outputs": {key: meta["value"] for key, meta in effective.items()},
        "conflicts": conflicts,
        "warnings": warnings,
    }
