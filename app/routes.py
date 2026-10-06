from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from . import services
from .db import get_session
from .errors import ApiError
from .schemas import PublishCommand, RevokeCommand

router = APIRouter()


def _norm_at(at: datetime | None) -> datetime:
    """查询时刻缺省为当前时间；无时区信息按 UTC 处理。"""
    if at is None:
        return datetime.now(timezone.utc)
    if at.tzinfo is None:
        return at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc)


@router.post("/api/v1/commands/publish", tags=["commands"], summary="提交发布命令")
async def publish(
    cmd: PublishCommand, session: AsyncSession = Depends(get_session)
) -> dict:
    return await services.execute_publish(session, cmd)


@router.post("/api/v1/commands/revoke", tags=["commands"], summary="提交撤销命令")
async def revoke(
    cmd: RevokeCommand, session: AsyncSession = Depends(get_session)
) -> dict:
    return await services.execute_revoke(session, cmd)


@router.get(
    "/api/v1/broadcasts",
    tags=["query"],
    summary="查询指定时刻各区域应播发的告警",
)
async def broadcasts(
    at: datetime | None = Query(
        default=None, description="查询时刻（ISO 8601），缺省为当前时间"
    ),
    region: str | None = Query(
        default=None, min_length=1, max_length=128, description="缺省返回全部区域"
    ),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if region is not None:
        region = region.strip()
        if not region:
            raise ApiError(422, "validation_error", "region 参数不能为空")
    return await services.query_broadcasts(session, _norm_at(at), region)


@router.get("/api/v1/alerts/{alert_no}", tags=["query"], summary="查询告警详情")
async def get_alert(alert_no: int, session: AsyncSession = Depends(get_session)) -> dict:
    return await services.get_alert_view(session, alert_no)


@router.get("/health", tags=["meta"], summary="健康检查")
async def health(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
