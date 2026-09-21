"""FastAPI 应用：规则集版本化管理与规则决策计算。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import crud
from .database import connect, init_db
from .engine import (
    CompiledRuleset,
    EvaluationFailed,
    RulesetCompileError,
    compile_ruleset,
    evaluate,
)
from .schemas import (
    ErrorResponse,
    EvaluateRequest,
    EvaluationOut,
    RecomputeRequest,
    RulesetCreate,
    RulesetMeta,
    RulesetUpdate,
    RulesetVersionOut,
)
from .seed import seed_demo


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = connect()
    try:
        init_db(conn)
        if os.environ.get("SEED_DEMO", "").strip() in ("1", "true", "TRUE", "yes"):
            seed_demo(conn)
            conn.commit()
    finally:
        conn.close()
    yield


app = FastAPI(
    title="规则决策服务",
    description=(
        "创建带条件依赖与冲突策略的规则集，提交文档按依赖图求稳定结果，"
        "返回命中顺序与冲突解释；支持循环依赖检测与规则集版本化。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ---------- 依赖 ----------


def get_db() -> sqlite3.Connection:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 异常映射 ----------


@app.exception_handler(RulesetCompileError)
async def handle_compile_error(_: Request, exc: RulesetCompileError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(EvaluationFailed)
async def handle_evaluation_failed(_: Request, exc: EvaluationFailed) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


def _error(code: str, message: str, status: int, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


# ---------- 工具 ----------


def _ruleset_meta(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "default_conflict_policy": row["default_conflict_policy"],
        "current_version": row["current_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _version_out(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": snapshot["id"],
        "ruleset_id": snapshot["ruleset_id"],
        "version": snapshot["version"],
        "name": snapshot["name"],
        "description": snapshot["description"],
        "default_conflict_policy": snapshot["default_conflict_policy"],
        "evaluation_order": snapshot["evaluation_order"],
        "created_at": snapshot["created_at"],
        "rules": [
            {
                "code": r["code"],
                "name": r["name"],
                "description": r.get("description", ""),
                "priority": r.get("priority", 0),
                "conflict_group": r.get("conflict_group"),
                "conflict_policy": r.get("conflict_policy"),
                "depends_on": r.get("depends_on", []),
                "condition": r.get("condition", ""),
                "action": r.get("action"),
                "referenced_rules": r.get("referenced_rules", []),
            }
            for r in snapshot["rules"]
        ],
    }


def _load_compiled(
    conn: sqlite3.Connection, ruleset_id: str, version: int
) -> tuple[dict[str, Any], CompiledRuleset]:
    """读取指定版本快照并编译为可求值对象。"""
    snapshot = crud.get_version(conn, ruleset_id, version)
    if snapshot is None:
        raise LookupError(f"规则集 {ruleset_id} 的版本 {version} 不存在")
    compiled = compile_ruleset(
        snapshot["name"],
        snapshot["description"],
        snapshot["default_conflict_policy"],
        snapshot["rules"],
    )
    return snapshot, compiled


# ---------- 健康检查 ----------


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------- 规则集 ----------


@app.post(
    "/api/rulesets",
    response_model=RulesetVersionOut,
    status_code=201,
    tags=["rulesets"],
    responses={400: {"model": ErrorResponse}},
)
def create_ruleset(
    payload: RulesetCreate, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    data = payload.model_dump()
    try:
        ruleset_id, version, _snapshot = crud.create_ruleset(conn, data)
    except KeyError as exc:
        return _error("RULESET_ID_EXISTS", str(exc.args[0]), 409)
    except crud.InvalidCustomId as exc:
        return _error("INVALID_ID", str(exc), 400)
    snapshot = crud.get_version(conn, ruleset_id, version)
    return _version_out(snapshot)


@app.get("/api/rulesets", response_model=list[RulesetMeta], tags=["rulesets"])
def list_rulesets(conn: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    return [_ruleset_meta(r) for r in crud.list_rulesets(conn)]


@app.get(
    "/api/rulesets/{ruleset_id}",
    response_model=RulesetMeta,
    tags=["rulesets"],
    responses={404: {"model": ErrorResponse}},
)
def get_ruleset(
    ruleset_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> Any:
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    return _ruleset_meta(row)


@app.put(
    "/api/rulesets/{ruleset_id}",
    response_model=RulesetVersionOut,
    tags=["rulesets"],
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def update_ruleset(
    ruleset_id: str,
    payload: RulesetUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    new_version = crud.update_ruleset(conn, ruleset_id, payload.model_dump())
    snapshot = crud.get_version(conn, ruleset_id, new_version)
    return _version_out(snapshot)


@app.get(
    "/api/rulesets/{ruleset_id}/versions",
    tags=["rulesets"],
    responses={404: {"model": ErrorResponse}},
)
def list_versions(
    ruleset_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> Any:
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    rows = crud.list_versions(conn, ruleset_id)
    return [
        {
            "id": r["id"],
            "ruleset_id": r["ruleset_id"],
            "version": r["version"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get(
    "/api/rulesets/{ruleset_id}/versions/{version}",
    response_model=RulesetVersionOut,
    tags=["rulesets"],
    responses={404: {"model": ErrorResponse}},
)
def get_version(
    ruleset_id: str,
    version: int,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    snapshot = crud.get_version(conn, ruleset_id, version)
    if snapshot is None:
        if crud.get_ruleset(conn, ruleset_id) is None:
            return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
        return _error("VERSION_NOT_FOUND", f"版本 {version} 不存在", 404)
    return _version_out(snapshot)


# ---------- 决策计算 ----------


@app.post(
    "/api/rulesets/{ruleset_id}/evaluate",
    response_model=EvaluationOut,
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def evaluate_document(
    ruleset_id: str,
    req: EvaluateRequest,
    version: int | None = Query(None, ge=1, description="指定版本；默认最新版"),
    persist: bool = Query(True, description="是否持久化本次结果"),
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)

    target_version = version or row["current_version"]
    try:
        _snapshot, compiled = _load_compiled(conn, ruleset_id, target_version)
    except LookupError:
        return _error(
            "VERSION_NOT_FOUND", f"版本 {target_version} 不存在", 404
        )

    result = evaluate(compiled, req.document)

    if persist:
        record = crud.save_evaluation(
            conn, ruleset_id, target_version, req.document, result
        )
    else:
        record = {
            "id": "",
            "ruleset_id": ruleset_id,
            "version": target_version,
            "document": req.document,
            "created_at": crud.now(),
            **result,
        }
    return record


@app.post(
    "/api/rulesets/{ruleset_id}/recompute",
    response_model=EvaluationOut,
    tags=["evaluation"],
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
def recompute(
    ruleset_id: str,
    req: RecomputeRequest,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """用最新版本重算。文档取请求体；为空则重放该规则集最近一次决策的文档。"""
    row = crud.get_ruleset(conn, ruleset_id)
    if row is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)

    document = req.document
    if document is None:
        document = crud.latest_evaluation_document(conn, ruleset_id)
        if document is None:
            return _error(
                "NO_DOCUMENT_TO_RECOMPUTE",
                "请求体未提供 document，且该规则集没有历史决策文档可重放",
                400,
            )

    target_version = row["current_version"]
    _snapshot, compiled = _load_compiled(conn, ruleset_id, target_version)
    result = evaluate(compiled, document)
    # 重算产生新记录，旧结果依旧保留
    return crud.save_evaluation(conn, ruleset_id, target_version, document, result)


@app.get(
    "/api/rulesets/{ruleset_id}/evaluations",
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}},
)
def list_evaluations(
    ruleset_id: str,
    limit: int = Query(100, ge=1, le=500),
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    if crud.get_ruleset(conn, ruleset_id) is None:
        return _error("NOT_FOUND", f"规则集不存在：{ruleset_id}", 404)
    return crud.list_evaluations(conn, ruleset_id, limit=limit)


@app.get(
    "/api/evaluations/{evaluation_id}",
    response_model=EvaluationOut,
    tags=["evaluation"],
    responses={404: {"model": ErrorResponse}},
)
def get_evaluation(
    evaluation_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> Any:
    record = crud.get_evaluation(conn, evaluation_id)
    if record is None:
        return _error("NOT_FOUND", f"决策结果不存在：{evaluation_id}", 404)
    return record
