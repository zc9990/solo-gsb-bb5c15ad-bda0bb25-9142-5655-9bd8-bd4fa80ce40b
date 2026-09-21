"""HTTP API integration tests."""

RULESET_PAYLOAD = {
    "name": "Loan test",
    "conflict_policy": "priority",
    "rules": [
        {
            "code": "R_ADULT",
            "name": "Adult",
            "condition": "doc.age >= 18",
            "decision": {"adult": True},
        },
        {
            "code": "R_APPROVE",
            "name": "Approve adult",
            "condition": "derived.adult == True",
            "depends_on": ["R_ADULT"],
            "decision": {"action": "approve"},
            "priority": 200,
        },
        {
            "code": "R_BLOCK",
            "name": "Blocked list",
            "condition": "doc.blocked == True",
            "decision": {"action": "deny"},
            "priority": 800,
        },
    ],
}


def _create(client, **overrides):
    payload = {**RULESET_PAYLOAD, **overrides}
    resp = client.post("/rulesets", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_and_get_ruleset(client):
    data = _create(client)
    rs = data["ruleset"]
    assert rs["current_version"] == 1
    got = client.get(f"/rulesets/{rs['id']}").json()
    assert got["version"]["version"] == 1
    assert len(got["version"]["rules"]) == 3


def test_cycle_rejected_on_save_with_422(client):
    rules = [
        {"code": "A", "condition": "True", "depends_on": ["B"]},
        {"code": "B", "condition": "True", "depends_on": ["A"]},
    ]
    resp = client.post("/rulesets", json={"name": "cyclic", "rules": rules})
    assert resp.status_code == 422
    body = resp.json()
    assert body["cycle"]
    assert body["cycle"][0] == body["cycle"][-1]
    # Nothing was persisted
    assert all(r["name"] != "cyclic" for r in client.get("/rulesets").json()["rulesets"])


def test_unknown_dependency_rejected(client):
    resp = client.post(
        "/rulesets",
        json={"name": "bad", "rules": [{"code": "A", "condition": "True", "depends_on": ["Z"]}]},
    )
    assert resp.status_code == 422
    assert "unknown rule" in resp.json()["detail"][0]


def test_evaluate_happy_path_and_conflict(client):
    rs_id = _create(client)["ruleset"]["id"]

    # Adult, not blocked -> approve
    ok = client.post(
        f"/rulesets/{rs_id}/evaluate", json={"content": {"age": 30, "blocked": False}}
    )
    assert ok.status_code == 201, ok.text
    body = ok.json()
    assert body["version"] == 1
    assert body["result"]["outputs"]["action"] == "approve"
    assert body["result"]["hit_order"] == ["R_ADULT", "R_APPROVE"]
    assert body["result"]["conflicts"] == []
    decision_id = body["decision_id"]

    # Adult AND blocked -> conflict, priority 800 deny wins
    blocked = client.post(
        f"/rulesets/{rs_id}/evaluate", json={"content": {"age": 30, "blocked": True}}
    ).json()
    result = blocked["result"]
    assert result["outputs"]["action"] == "deny"
    conflict = result["conflicts"][0]
    assert conflict["output"] == "action"
    assert conflict["winner"]["rule"] == "R_BLOCK"
    assert any(l["rule"] == "R_APPROVE" for l in conflict["losers"])

    # Underage: nothing fires
    young = client.post(
        f"/rulesets/{rs_id}/evaluate", json={"content": {"age": 10}}
    ).json()
    assert young["result"]["outputs"] == {}

    # Old result is still queryable verbatim
    fetched = client.get(f"/decisions/{decision_id}").json()
    assert fetched["decision"]["result"]["outputs"]["action"] == "approve"
    assert fetched["document"]["content"]["age"] == 30


def test_update_creates_new_version_and_old_results_survive(client):
    created = _create(client)
    rs_id = created["ruleset"]["id"]

    decision_v1 = client.post(
        f"/rulesets/{rs_id}/evaluate", json={"content": {"age": 30}}
    ).json()
    assert decision_v1["version"] == 1

    # Modify: raise age threshold to 65
    updated = client.put(
        f"/rulesets/{rs_id}",
        json={
            "rules": [
                {
                    "code": "R_ADULT",
                    "condition": "doc.age >= 65",
                    "decision": {"adult": True},
                },
                {
                    "code": "R_APPROVE",
                    "condition": "derived.adult == True",
                    "depends_on": ["R_ADULT"],
                    "decision": {"action": "approve"},
                    "priority": 200,
                },
            ]
        },
    ).json()
    assert updated["version"]["version"] == 2
    assert updated["ruleset"]["current_version"] == 2

    # Current evaluation under new rules: age 30 no longer approves
    new_eval = client.post(
        f"/rulesets/{rs_id}/evaluate", json={"content": {"age": 30}}
    ).json()
    assert new_eval["version"] == 2
    assert new_eval["result"]["outputs"] == {}

    # Re-evaluating against the OLD version still works and persists v1 result
    old_eval = client.post(
        f"/rulesets/{rs_id}/versions/1/evaluate", json={"content": {"age": 30}}
    ).json()
    assert old_eval["version"] == 1
    assert old_eval["result"]["outputs"]["action"] == "approve"

    # Version listing + old decision retrieval
    versions = client.get(f"/rulesets/{rs_id}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]

    old_decision = client.get(f"/decisions/{decision_v1['decision_id']}").json()
    assert old_decision["decision"]["version"] == 1

    listing = client.get(f"/rulesets/{rs_id}/decisions").json()["decisions"]
    assert {d["version"] for d in listing} == {1, 2}
    v1_only = client.get(f"/rulesets/{rs_id}/decisions?version=1").json()["decisions"]
    assert all(d["version"] == 1 for d in v1_only)


def test_update_with_cycle_is_rejected_and_keeps_old_version(client):
    rs_id = _create(client)["ruleset"]["id"]
    resp = client.put(
        f"/rulesets/{rs_id}",
        json={
            "rules": [
                {"code": "A", "condition": "True", "depends_on": ["B"]},
                {"code": "B", "condition": "True", "depends_on": ["A"]},
            ]
        },
    )
    assert resp.status_code == 422
    rs = client.get(f"/rulesets/{rs_id}").json()
    assert rs["ruleset"]["current_version"] == 1


def test_dry_run_does_not_persist(client):
    before = len(client.get("/rulesets").json()["rulesets"])
    resp = client.post(
        "/evaluate/dry-run",
        json={
            "conflict_policy": "collect_all",
            "rules": [{"code": "A", "condition": "doc.x > 0", "decision": {"ok": True}}],
            "document": {"x": 1},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["outputs"] == {"ok": True}
    after = len(client.get("/rulesets").json()["rulesets"])
    assert before == after


def test_404_for_unknown_entities(client):
    assert client.get("/rulesets/nope").status_code == 404
    assert client.get("/decisions/nope").status_code == 404
    rs_id = _create(client)["ruleset"]["id"]
    assert client.get(f"/rulesets/{rs_id}/versions/99").status_code == 404


def test_seed_endpoint_is_idempotent(client):
    first = client.post("/admin/seed").json()
    second = client.post("/admin/seed").json()
    assert first["created"] is True
    assert second["created"] is False
