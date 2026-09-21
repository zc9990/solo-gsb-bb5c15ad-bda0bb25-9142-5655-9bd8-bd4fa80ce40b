"""HTTP API for the rule decision service."""

from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from . import engine, seed as seed_module, store
from .models import (
    DocumentIn,
    DryEvaluateIn,
    RuleSetCreate,
    RuleSetSummary,
    RuleSetUpdate,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    yield


app = FastAPI(
    title="Rule Decision Service",
    version="1.0.0",
    description=(
        "Create versioned rule sets with conditional dependencies and conflict "
        "policies, submit documents and run dependency-graph fixed-point decisions."
    ),
    lifespan=lifespan,
)


def db(request: Request):
    """One SQLite connection per request."""
    conn = store.connect()
    try:
        yield conn
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


@app.exception_handler(engine.RuleSetValidationError)
async def _validation_handler(_request: Request, exc: engine.RuleSetValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors, "cycle": exc.cycle},
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _rules_from_payload(rules: list[Any]) -> list[engine.RuleSpec]:
    return engine.parse_rules([r.model_dump() for r in rules])


def _require_ruleset(conn, ruleset_id: str) -> dict[str, Any]:
    rs = store.get_ruleset(conn, ruleset_id)
    if rs is None:
        raise HTTPException(status_code=404, detail=f"Rule set {ruleset_id} not found")
    return rs


def _summary(rs: dict[str, Any]) -> dict[str, Any]:
    return RuleSetSummary(
        id=rs["id"],
        name=rs["name"],
        description=rs["description"],
        current_version=rs["current_version"],
        created_at=rs["created_at"],
        updated_at=rs["updated_at"],
    ).model_dump()


def _run_and_persist(
    conn, *, rules: list[engine.RuleSpec], policy: str, document: dict[str, Any],
    ruleset_id: str, version: int, external_id: str | None,
) -> dict[str, Any]:
    result = engine.evaluate(rules, policy, document)
    doc_row = store.insert_document(conn, document, external_id)
    decision_row = store.save_decision(
        conn,
        ruleset_id=ruleset_id,
        version=version,
        document_id=doc_row["id"],
        result=result,
    )
    return {
        "decision_id": decision_row["id"],
        "ruleset_id": ruleset_id,
        "version": version,
        "document_id": doc_row["id"],
        "document": doc_row["content"],
        "created_at": decision_row["created_at"],
        "result": result,
    }


# --------------------------------------------------------------------------- #
# Health & meta
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/policies", tags=["meta"])
def policies() -> dict[str, list[str]]:
    return {"conflict_policies": list(engine.CONFLICT_POLICIES)}


@app.post("/admin/seed", tags=["meta"])
def seed_demo_data(conn=Depends(db)) -> dict[str, Any]:
    """Idempotently insert a demo loan-approval rule set."""
    return seed_module.seed_if_empty(conn)


# --------------------------------------------------------------------------- #
# Rule sets
# --------------------------------------------------------------------------- #


@app.post("/rulesets", status_code=201, tags=["rulesets"])
def create_ruleset(payload: RuleSetCreate, conn=Depends(db)) -> dict[str, Any]:
    rules = _rules_from_payload(payload.rules)  # raises 422 on cycle/invalid
    rs = store.create_ruleset(
        conn,
        name=payload.name,
        description=payload.description,
        conflict_policy=payload.conflict_policy,
        rules=[r.to_dict() for r in rules],
    )
    return {"ruleset": _summary(rs), "version": store.get_version(conn, rs["id"])}


@app.get("/rulesets", tags=["rulesets"])
def list_rulesets(conn=Depends(db)) -> dict[str, list[dict[str, Any]]]:
    return {"rulesets": [_summary(rs) for rs in store.list_rulesets(conn)]}


@app.get("/rulesets/{ruleset_id}", tags=["rulesets"])
def get_ruleset(ruleset_id: str, conn=Depends(db)) -> dict[str, Any]:
    rs = _require_ruleset(conn, ruleset_id)
    version = store.get_version(conn, ruleset_id, rs["current_version"])
    return {"ruleset": _summary(rs), "version": version}


@app.put("/rulesets/{ruleset_id}", tags=["rulesets"])
def update_ruleset(ruleset_id: str, payload: RuleSetUpdate, conn=Depends(db)) -> dict[str, Any]:
    """Create a NEW version. The current version pointer moves; old results stay."""
    rs = _require_ruleset(conn, ruleset_id)
    current = store.get_version(conn, ruleset_id, rs["current_version"])

    if payload.rules is not None:
        rules_dicts = [r.to_dict() for r in _rules_from_payload(payload.rules)]
    else:
        rules_dicts = current["rules"]

    policy = payload.conflict_policy or current["conflict_policy"]
    if policy not in engine.CONFLICT_POLICIES:
        raise HTTPException(
            status_code=422,
            detail=[f"Unknown conflict_policy {policy!r}"],
        )

    new_version = store.add_version(
        conn,
        ruleset_id,
        conflict_policy=policy,
        rules=rules_dicts,
        name=payload.name,
        description=payload.description,
    )
    refreshed = store.get_ruleset(conn, ruleset_id)
    return {
        "ruleset": _summary(refreshed),
        "version": new_version,
        "previous_version": rs["current_version"],
        "message": (
            "New version created; decisions for the previous version "
            f"({rs['current_version']}) remain stored and queryable."
        ),
    }


@app.get("/rulesets/{ruleset_id}/versions", tags=["rulesets"])
def get_versions(ruleset_id: str, conn=Depends(db)) -> dict[str, Any]:
    _require_ruleset(conn, ruleset_id)
    return {"versions": store.list_versions(conn, ruleset_id)}


@app.get("/rulesets/{ruleset_id}/versions/{version}", tags=["rulesets"])
def get_one_version(ruleset_id: str, version: int, conn=Depends(db)) -> dict[str, Any]:
    _require_ruleset(conn, ruleset_id)
    data = store.get_version(conn, ruleset_id, version)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")
    return {"ruleset_id": ruleset_id, "version": data}


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


@app.post("/rulesets/{ruleset_id}/evaluate", status_code=201, tags=["decisions"])
def evaluate_current(
    ruleset_id: str,
    payload: DocumentIn,
    conn=Depends(db),
) -> dict[str, Any]:
    """Evaluate a document against the CURRENT version and persist the result."""
    rs = _require_ruleset(conn, ruleset_id)
    version_row = store.get_version(conn, ruleset_id, rs["current_version"])
    rules = engine.parse_rules(version_row["rules"])
    return _run_and_persist(
        conn,
        rules=rules,
        policy=version_row["conflict_policy"],
        document=payload.content,
        ruleset_id=ruleset_id,
        version=rs["current_version"],
        external_id=payload.external_id,
    )


@app.post(
    "/rulesets/{ruleset_id}/versions/{version}/evaluate",
    status_code=201,
    tags=["decisions"],
)
def evaluate_version(
    ruleset_id: str,
    version: int,
    payload: DocumentIn,
    conn=Depends(db),
) -> dict[str, Any]:
    """Evaluate (and store) a decision against a specific, possibly old, version."""
    _require_ruleset(conn, ruleset_id)
    version_row = store.get_version(conn, ruleset_id, version)
    if version_row is None:
        raise HTTPException(status_code=404, detail=f"Version {version} not found")
    rules = engine.parse_rules(version_row["rules"])
    return _run_and_persist(
        conn,
        rules=rules,
        policy=version_row["conflict_policy"],
        document=payload.content,
        ruleset_id=ruleset_id,
        version=version,
        external_id=payload.external_id,
    )


@app.post("/evaluate/dry-run", tags=["decisions"])
def dry_run(payload: DryEvaluateIn) -> dict[str, Any]:
    """Validate rules and compute a decision without persisting anything."""
    rules = _rules_from_payload(payload.rules)
    result = engine.evaluate(rules, payload.conflict_policy, payload.document)
    return {"result": result}


@app.get("/rulesets/{ruleset_id}/decisions", tags=["decisions"])
def list_decisions(
    ruleset_id: str,
    version: int | None = Query(None, description="Filter by rule set version"),
    limit: int = Query(50, ge=1, le=500),
    conn=Depends(db),
) -> dict[str, Any]:
    _require_ruleset(conn, ruleset_id)
    return {"decisions": store.list_decisions(conn, ruleset_id, version=version, limit=limit)}


@app.get("/decisions/{decision_id}", tags=["decisions"])
def get_decision(decision_id: str, conn=Depends(db)) -> dict[str, Any]:
    data = store.get_decision(conn, decision_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    doc = store.get_document(conn, data["document_id"])
    return {"decision": data, "document": doc}
