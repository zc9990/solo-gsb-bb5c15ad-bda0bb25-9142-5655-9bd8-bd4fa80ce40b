# Rule Decision Service

基于 **FastAPI + SQLite + Docker** 的规则决策服务。用户可以创建带有**条件依赖**和**冲突策略**的规则集，提交文档（document）进行决策计算；系统按依赖图求**稳定结果（fixed point）**，返回命中规则顺序与冲突解释，在保存时**检测循环依赖并拒绝**。规则集支持**版本化**：修改产生新版本并自动重算，旧版本及其历史结果永久可查。

---

## 功能特性

- **安全条件表达式**：AST 白名单求值（不使用 `eval`），支持比较、布尔/算术运算、三元表达式、`in`、下标/属性访问、列表/字典字面量；禁止函数调用、双下划线属性、导入等。
- **条件依赖图**：规则通过 `depends_on` 声明对其他规则输出（`derived.*`）的依赖。
  - 保存时做引用校验与 **DFS 循环检测**，发现环返回 `422` 及环路径（如 `A -> B -> A`），不落库。
  - 求值按 **Kahn 拓扑序** 执行；依赖其他规则输出的规则会在后续轮次被触发，**迭代到不动点**（无环图保证收敛），返回 `stable`、轮次数、评估顺序与命中顺序。
- **四种冲突策略**（同一输出被写成不同值时）：
  - `priority`（默认）：`priority` 数值最大者胜；
  - `deny_overrides`：`action` 输出中 `deny` 永远压倒其它动作，其余键按优先级；
  - `first_hit`：仅按拓扑序执行第一条命中的规则；
  - `collect_all`：允许覆盖，最后一次写入生效。
  - 每个冲突都返回胜出方、失败方、同值方及人类可读的解释。
- **规则集版本化**：`PUT` 修改（规则/策略/名称）总是创建新版本并更新当前版本指针；历史版本不可变，所有旧决策结果保留、可按版本筛选查询。可用指定旧版本重新计算。
- **持久化**：SQLite（`rulesets / ruleset_versions / documents / decisions` 四张表），数据卷挂载到 `/data`。
- **无状态试算**：`POST /evaluate/dry-run` 校验+计算但不保存。
- 内置幂等种子数据（贷款审批示例，含依赖链与冲突）。

---

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务: http://localhost:8000  交互式文档: http://localhost:8000/docs
```

启动时自动建表，`SEED_DEMO_DATA=true`（compose 默认）会写入一个演示规则集。数据保存在命名卷 `rule-data` 中。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
export RULES_DB_PATH=./data/rules.db
export SEED_DEMO_DATA=true          # 可选：写入演示数据
bash entrypoint.sh                  # 建表 + 可选 seed + 启动 uvicorn
```

### 运行测试

```bash
python3 -m pytest tests/ -q
```

---

## 规则表达式

每条规则形如：

```json
{
  "code": "R_APPROVE",
  "name": "自动通过",
  "condition": "derived.eligible == true and derived.score_band == \"high\"",
  "decision": {"risk": "low", "action": "approve", "tag": "= doc.name + '/ok'"},
  "depends_on": ["R_BASELINE"],
  "priority": 200
}
```

- `condition`：布尔表达式，命中时把 `decision` 写入决策输出。
  - `doc.xxx` / `doc['xxx']`：输入文档字段；
  - `derived.xxx` / 裸字段名：其他规则已产生的输出；
  - 缺失字段遵循 SQL 风格 NULL 语义（比较结果为 false，不抛异常）。
- `decision`：常量，或以 `=` 开头的动态表达式。
- `depends_on`：依赖的规则 code 列表；依赖图必须是 **DAG**。
- `priority`：整数，越大越优先（用于 `priority` / `deny_overrides`）。

---

## HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/policies` | 支持的冲突策略 |
| POST | `/admin/seed` | 幂等写入演示规则集 |
| POST | `/rulesets` | 创建规则集（校验失败/含环 → `422`，不落库） |
| GET | `/rulesets` | 列出规则集 |
| GET | `/rulesets/{id}` | 获取规则集及当前版本内容 |
| PUT | `/rulesets/{id}` | 修改 → **创建新版本**，旧结果保留 |
| GET | `/rulesets/{id}/versions` | 版本列表 |
| GET | `/rulesets/{id}/versions/{v}` | 获取指定版本 |
| POST | `/evaluate/dry-run` | 试算（不落库） |
| POST | `/rulesets/{id}/evaluate` | 用**当前版本**计算并保存结果 |
| POST | `/rulesets/{id}/versions/{v}/evaluate` | 用**指定版本**计算并保存 |
| GET | `/rulesets/{id}/decisions?version=&limit=` | 决策历史（可按版本过滤） |
| GET | `/decisions/{decision_id}` | 查询某次决策（含文档与完整结果） |

### 示例：创建规则集

```bash
curl -s -X POST localhost:8000/rulesets \
  -H 'Content-Type: application/json' -d '{
  "name": "loan",
  "conflict_policy": "priority",
  "rules": [
    {"code": "R_AGE",  "condition": "doc.age >= 18",
     "decision": {"adult": true}},
    {"code": "R_BASE", "condition": "derived.adult == true and doc.credit_score >= 600",
     "depends_on": ["R_AGE"], "decision": {"eligible": true}},
    {"code": "R_OK",   "condition": "derived.eligible == true",
     "depends_on": ["R_BASE"], "decision": {"action": "approve"}, "priority": 200},
    {"code": "R_DENY", "condition": "doc.blacklisted == true",
     "decision": {"action": "deny"}, "priority": 800}
  ]}'
```

### 示例：提交文档并决策

```bash
curl -s -X POST localhost:8000/rulesets/$RS_ID/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"content": {"age": 30, "credit_score": 750, "blacklisted": true},
       "external_id": "case-2026-001"}'
```

响应（节选）：

```json
{
  "version": 1,
  "result": {
    "stable": true,
    "rounds": 2,
    "evaluation_order": ["R_AGE", "R_DENY", "R_BASE", "R_OK"],
    "hit_order": ["R_AGE", "R_DENY", "R_BASE", "R_OK"],
    "outputs": {"adult": true, "eligible": true, "action": "deny"},
    "conflicts": [{
      "output": "action",
      "policy": "priority",
      "winner": {"rule": "R_DENY", "value": "deny", "priority": 800},
      "losers": [{"rule": "R_OK", "value": "approve", "priority": 200}],
      "agreements": [],
      "message": "Conflict on output 'action': R_DENY='deny' wins over R_OK='approve' because rule 'R_DENY' has the highest priority (800)."
    }]
  }
}
```

### 示例：循环依赖被拒绝

```bash
curl -i -X POST localhost:8000/rulesets -H 'Content-Type: application/json' -d '{
  "name": "cyc",
  "rules": [
    {"code": "A", "condition": "True", "depends_on": ["B"]},
    {"code": "B", "condition": "True", "depends_on": ["A"]}
  ]}'
# HTTP 422
# {"detail": ["Cyclic dependency detected: A -> B -> A"],
#  "cycle":  ["A", "B", "A"]}
```

### 示例：修改产生新版本，旧结果可查

```bash
# 修改规则或策略（只传需要变更的字段）
curl -X PUT localhost:8000/rulesets/$RS_ID \
  -H 'Content-Type: application/json' -d '{"conflict_policy": "deny_overrides"}'

# 当前版本的决策自动使用新版本
curl -X POST localhost:8000/rulesets/$RS_ID/evaluate -d '{"content": {...}}' ...
# 仍可用旧版本复算
curl -X POST localhost:8000/rulesets/$RS_ID/versions/1/evaluate -d '{"content": {...}}' ...
# 查询历史（含旧版本结果）
curl "localhost:8000/rulesets/$RS_ID/decisions?version=1"
```

---

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由与生命周期
│   ├── models.py      # Pydantic 请求/响应模型
│   ├── engine.py      # 安全表达式、依赖图/环检测、不动点求值、冲突解释
│   ├── store.py       # SQLite 建表与 CRUD（规则集/版本/文档/决策）
│   └── seed.py        # 幂等演示数据
├── tests/             # pytest：引擎单测 + HTTP API 集成测试（25 个用例）
├── entrypoint.sh      # 建表 + 可选 seed + 启动
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 求值语义说明

1. 规则按拓扑序评估；命中过的规则不重复触发。
2. 若本轮有新规则因上游输出而命中，则继续下一轮，直到无人再命中（稳定结果）。
3. 冲突在写入同一输出且值不同时产生：是否替换当前值取决于策略；无论是否替换，每次尝试都会记录，最终生成冲突解释。
4. 有向无环图保证求值在 `N+1` 轮内收敛；若未收敛，响应中 `stable=false`（正常规则集不会发生）。
