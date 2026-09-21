def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_demo_seeded_on_startup(client, demo_id):
    resp = client.get(f"/api/rulesets/{demo_id}")
    assert resp.status_code == 200
    assert resp.json()["current_version"] == 1


def test_create_and_evaluate_simple(client, simple_ruleset_payload):
    resp = client.post("/api/rulesets", json=simple_ruleset_payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    ruleset_id = body["ruleset_id"]
    assert body["version"] == 1
    assert body["evaluation_order"] == ["r1", "r2"]

    resp = client.post(
        f"/api/rulesets/{ruleset_id}/evaluate", json={"document": {"x": 42}}
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["fired_rule_codes"] == ["r1", "r2"]
    assert result["hit_rule_codes"] == ["r1", "r2"]
    assert result["decision"] == {"level": "high", "bonus": 1}
    assert result["iterations"] == 2
    assert result["id"]


def test_evaluate_non_matching_document(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    result = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 1}}
    ).json()
    assert result["fired_rule_codes"] == []
    assert result["decision"] == {}
    assert result["iterations"] == 1


def test_cyclic_ruleset_rejected_on_create(client):
    payload = {
        "name": "cyclic",
        "rules": [
            {"code": "a", "condition": "b.hit", "depends_on": ["b"]},
            {"code": "b", "condition": "a.hit == true", "depends_on": ["a"]},
        ],
    }
    resp = client.post("/api/rulesets", json=payload)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "CYCLIC_DEPENDENCY"
    assert error["details"]["cycle"][0] == error["details"]["cycle"][-1]


def test_cyclic_ruleset_rejected_on_update(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    payload = dict(simple_ruleset_payload)
    payload["rules"] = [
        {"code": "a", "condition": "b.hit", "depends_on": ["b"]},
        {"code": "b", "condition": "a.hit", "depends_on": ["a"]},
    ]
    resp = client.put(f"/api/rulesets/{rid}", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CYCLIC_DEPENDENCY"
    # 更新失败，当前版本仍是 1
    meta = client.get(f"/api/rulesets/{rid}").json()
    assert meta["current_version"] == 1


def test_update_creates_new_version_and_keeps_old(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]

    # 旧版本下评估并保存结果
    old_eval = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    assert old_eval["version"] == 1

    # 修改：提高阈值并加新规则
    updated = {
        "name": "simple",
        "rules": [
            {"code": "r1", "condition": 'doc["x"] >= 1000',
             "action": '{"level": "high"}'},
            {"code": "r2", "condition": "r1.hit", "depends_on": ["r1"],
             "action": '{"bonus": 1}'},
        ],
    }
    resp = client.put(f"/api/rulesets/{rid}", json=updated)
    assert resp.status_code == 200
    assert resp.json()["version"] == 2

    meta = client.get(f"/api/rulesets/{rid}").json()
    assert meta["current_version"] == 2

    # 旧版本快照仍可查
    v1 = client.get(f"/api/rulesets/{rid}/versions/1").json()
    assert v1["rules"][0]["condition"] == 'doc["x"] >= 10'
    v2 = client.get(f"/api/rulesets/{rid}/versions/2").json()
    assert v2["rules"][0]["condition"] == 'doc["x"] >= 1000'

    # 版本列表
    versions = client.get(f"/api/rulesets/{rid}/versions").json()
    assert [v["version"] for v in versions] == [1, 2]

    # 同一文档在最新版上不再命中，但可用旧版重算
    new_eval = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    assert new_eval["version"] == 2
    assert new_eval["fired_rule_codes"] == []

    old_again = client.post(
        f"/api/rulesets/{rid}/evaluate?version=1",
        json={"document": {"x": 100}},
    ).json()
    assert old_again["version"] == 1
    assert old_again["fired_rule_codes"] == ["r1", "r2"]


def test_old_evaluations_remain_queryable(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    e1 = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    client.put(
        f"/api/rulesets/{rid}",
        json={**simple_ruleset_payload, "name": "simple2"},
    )
    e2 = client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    ).json()
    assert e2["version"] == 2

    listing = client.get(f"/api/rulesets/{rid}/evaluations").json()
    assert {e["id"] for e in listing} == {e1["id"], e2["id"]}
    assert {e["version"] for e in listing} == {1, 2}

    got = client.get(f"/api/evaluations/{e1['id']}").json()
    assert got["version"] == 1
    assert got["document"] == {"x": 100}


def test_recompute_with_latest_version(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    client.post(
        f"/api/rulesets/{rid}/evaluate", json={"document": {"x": 100}}
    )
    # 规则收紧，x=100 在新版不命中
    client.put(
        f"/api/rulesets/{rid}",
        json={
            "name": "simple",
            "rules": [
                {"code": "r1", "condition": 'doc["x"] >= 1000'},
            ],
        },
    )
    # 不带 document，重放最近一次文档
    resp = client.post(f"/api/rulesets/{rid}/recompute", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 2
    assert body["document"] == {"x": 100}
    assert body["fired_rule_codes"] == []
    # 旧结果仍保留：共两条历史
    assert len(client.get(f"/api/rulesets/{rid}/evaluations").json()) == 2


def test_recompute_without_history_requires_document(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(f"/api/rulesets/{rid}/recompute", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "NO_DOCUMENT_TO_RECOMPUTE"


def test_evaluate_persist_flag(client, simple_ruleset_payload):
    rid = client.post("/api/rulesets", json=simple_ruleset_payload).json()["ruleset_id"]
    resp = client.post(
        f"/api/rulesets/{rid}/evaluate?persist=false",
        json={"document": {"x": 1}},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == ""
    assert client.get(f"/api/rulesets/{rid}/evaluations").json() == []


def test_conflict_explanations_in_response(client, demo_id):
    # VIP + 大额：discount 组中 vip_discount(priority20) 压制 large_order(10)
    resp = client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 1500, "customer": {"vip": True}, "region": "north"}},
    )
    result = resp.json()
    assert "vip_discount" in result["fired_rule_codes"]
    assert "large_order" in result["fired_rule_codes"]
    assert "large_order" not in result["hit_rule_codes"]
    assert "vip_discount" in result["hit_rule_codes"]
    discount_conflict = next(c for c in result["conflicts"] if c["group"] == "discount")
    assert discount_conflict["policy"] == "priority"
    assert discount_conflict["winner"] == "vip_discount"
    suppressed_codes = [s["rule_code"] for s in discount_conflict["suppressed"]]
    assert suppressed_codes == ["large_order"]
    assert result["decision"]["discount"] == 0.15
    assert result["decision"]["free_shipping"] is True


def test_first_hit_coupon_conflict(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 10, "coupons": ["SAVE10", "SAVE20"]}},
    )
    result = resp.json()
    coupon_conflict = next(c for c in result["conflicts"] if c["group"] == "coupon")
    assert coupon_conflict["policy"] == "first_hit"
    assert coupon_conflict["winner"] == "coupon_save10"
    assert result["decision"]["coupon"] == "SAVE10"


def test_manual_review_for_non_vip_huge_order(client, demo_id):
    # 3000 但非 VIP：large_order 命中、vip_discount 不命中 -> 人工审核命中
    resp = client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 3000, "customer": {"vip": False}}},
    )
    result = resp.json()
    assert "manual_review" in result["hit_rule_codes"]
    assert result["decision"]["manual_review"] is True


def test_no_review_when_vip(client, demo_id):
    resp = client.post(
        f"/api/rulesets/{demo_id}/evaluate",
        json={"document": {"amount": 5000, "customer": {"vip": True}}},
    )
    result = resp.json()
    assert "manual_review" not in result["hit_rule_codes"]


def test_404_paths(client):
    assert client.get("/api/rulesets/nope").status_code == 404
    assert client.get("/api/rulesets/nope/versions").status_code == 404
    assert client.get("/api/rulesets/nope/versions/1").status_code == 404
    assert client.post(
        "/api/rulesets/nope/evaluate", json={"document": {}}
    ).status_code == 404
    assert client.get("/api/evaluations/nope").status_code == 404


def test_custom_id_and_duplicate(client):
    payload = {"id": "my-set", "name": "n", "rules": [{"code": "a", "condition": "true"}]}
    assert client.post("/api/rulesets", json=payload).status_code == 201
    dup = client.post("/api/rulesets", json=payload)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "RULESET_ID_EXISTS"


def test_undeclared_reference_rejected(client):
    payload = {
        "name": "n",
        "rules": [
            {"code": "a"},
            {"code": "b", "condition": "a.hit"},  # 缺少 depends_on
        ],
    }
    resp = client.post("/api/rulesets", json=payload)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "UNDECLARED_REFERENCE"
