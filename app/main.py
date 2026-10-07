"""园区应急广播播发裁决服务 HTTP API。

接口:
- POST /api/v1/commands/publish  发布告警
- POST /api/v1/commands/revoke   撤销告警
- GET  /api/v1/broadcasts        查询指定时刻各区域应播发内容
- GET  /healthz                  存活探针
- GET  /readyz                   就绪探针（含数据库检查）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import engine, get_session, init_db, wait_for_database
from .schemas import (
    BroadcastQueryResult,
    PublishCommand,
    PublishReceipt,
    RevokeCommand,
    RevokeReceipt,
)
from .service import (
    ConflictError,
    NotFoundError,
    ServiceError,
    accept_publish,
    accept_revoke,
    normalize_at,
    query_broadcasts,
    utcnow,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时等待数据库就绪并自动建表（compose healthcheck 的第二道保险）
    wait_for_database()
    init_db()
    yield


app = FastAPI(
    title="园区应急广播播发裁决服务",
    version="1.0.0",
    description="基于受理时间对发布/撤销命令进行幂等裁决，支持任意时刻区域播发查询。",
    lifespan=lifespan,
)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


@app.exception_handler(ServiceError)
async def _service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
    status_code = {
        ConflictError: status.HTTP_409_CONFLICT,
        NotFoundError: status.HTTP_404_NOT_FOUND,
    }.get(type(exc), status.HTTP_400_BAD_REQUEST)
    return _error_response(status_code, exc.code, exc.message)


@app.exception_handler(IntegrityError)
async def _integrity_error_handler(_: Request, exc: IntegrityError) -> JSONResponse:
    # 并发抢占告警号/请求号时的最后防线（SELECT FOR UPDATE 锁不住不存在的行）
    return _error_response(
        status.HTTP_409_CONFLICT,
        "conflict",
        "并发冲突或唯一约束冲突，请使用新的请求号/告警号重试",
    )


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    return _error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "validation_error",
        _format_validation(exc.errors()),
    )


def _format_validation(errors: list[dict[str, Any]]) -> str:
    parts = []
    for err in errors:
        loc = ".".join(str(p) for p in err.get("loc", []) if p != "body")
        parts.append(f"{loc or '请求体'}: {err.get('msg', '无效')}")
    return "; ".join(parts) or "请求参数无效"


@app.post(
    "/api/v1/commands/publish",
    response_model=PublishReceipt,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "请求号重试，返回首次回执"},
        201: {"description": "首次受理成功"},
        409: {"description": "请求号载荷冲突或告警号已存在"},
        422: {"description": "载荷校验失败"},
    },
    summary="发布告警（同一请求号幂等）",
)
def publish(
    cmd: PublishCommand, session: Session = Depends(get_session)
) -> JSONResponse:
    now = utcnow()
    try:
        receipt, first_time = accept_publish(session, cmd, now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if first_time else status.HTTP_200_OK,
        content=receipt.model_dump(mode="json"),
    )


@app.post(
    "/api/v1/commands/revoke",
    response_model=RevokeReceipt,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "请求号重试，返回首次回执"},
        201: {"description": "首次受理成功"},
        404: {"description": "撤销目标告警不存在"},
        409: {"description": "请求号载荷冲突或告警已撤销/到期"},
        422: {"description": "载荷校验失败"},
    },
    summary="撤销告警（按受理时间终止后续播发）",
)
def revoke(
    cmd: RevokeCommand, session: Session = Depends(get_session)
) -> JSONResponse:
    now = utcnow()
    try:
        receipt, first_time = accept_revoke(session, cmd, now)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if first_time else status.HTTP_200_OK,
        content=receipt.model_dump(mode="json"),
    )


@app.get(
    "/api/v1/broadcasts",
    response_model=BroadcastQueryResult,
    summary="查询指定时刻各区域应播发的告警",
)
def broadcasts(
    at: datetime | None = Query(
        default=None,
        description="裁决时刻（ISO 8601，如 2026-10-07T12:00:00Z）；缺省为当前 UTC 时刻",
    ),
    area: list[str] | None = Query(
        default=None, description="可选，仅查询指定区域，可重复传入或逗号分隔；缺省返回全部区域"
    ),
    session: Session = Depends(get_session),
) -> BroadcastQueryResult:
    moment = normalize_at(at)
    areas = _parse_areas(area)
    return query_broadcasts(session, moment, areas)


def _parse_areas(raw: list[str] | None) -> list[str] | None:
    if not raw:
        return None
    result: set[str] = set()
    for item in raw:
        for part in item.split(","):
            name = part.strip()
            if name:
                result.add(name)
    return sorted(result) or None


@app.get("/healthz", summary="存活探针")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", summary="就绪探针")
def readyz() -> JSONResponse:
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        return _error_response(503, "not_ready", "数据库不可用")
    return JSONResponse(status_code=200, content={"status": "ready"})
