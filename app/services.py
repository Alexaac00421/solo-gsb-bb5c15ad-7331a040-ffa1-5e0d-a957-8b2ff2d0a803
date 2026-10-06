from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import ApiError
from .models import Alert, AlertRegion, Command
from .schemas import PublishCommand, RevokeCommand


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def alert_view(alert: Alert) -> dict[str, Any]:
    return {
        "alert_no": alert.alert_no,
        "level": alert.level,
        "message": alert.message,
        "regions": sorted(r.region for r in alert.regions),
        "delay_seconds": alert.delay_seconds,
        "duration_seconds": alert.duration_seconds,
        "accepted_at": _iso(alert.accepted_at),
        "start_at": _iso(alert.start_at),
        "end_at": _iso(alert.end_at),
        "revoked_at": _iso(alert.revoked_at) if alert.revoked_at else None,
    }


async def _try_insert_command(
    session: AsyncSession,
    *,
    request_no: str,
    command_type: str,
    payload: dict,
    accepted_at: datetime,
) -> bool:
    """抢占幂等键。并发下同键插入会阻塞等待对方事务结束，随后返回 False。"""
    stmt = (
        pg_insert(Command)
        .values(
            request_no=request_no,
            command_type=command_type,
            payload=payload,
            receipt=None,
            accepted_at=accepted_at,
        )
        .on_conflict_do_nothing(index_elements=["request_no"])
        .returning(Command.request_no)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _replay_or_conflict(
    session: AsyncSession, *, request_no: str, payload: dict
) -> dict:
    """request_no 已存在：载荷一致返回首次回执，否则报 409 冲突。"""
    existing = await session.get(Command, request_no)
    if existing is not None and existing.payload == payload:
        return existing.receipt
    raise ApiError(
        409, "request_no_conflict", f"请求号 {request_no!r} 已被不同载荷的命令占用"
    )


async def _store_receipt(session: AsyncSession, *, request_no: str, receipt: dict) -> None:
    await session.execute(
        update(Command).where(Command.request_no == request_no).values(receipt=receipt)
    )


async def execute_publish(session: AsyncSession, cmd: PublishCommand) -> dict:
    """发布告警：以受理时间推算生效区间，多区域同事务写入，同成同败。"""
    accepted_at = datetime.now(timezone.utc)
    start_at = accepted_at + timedelta(seconds=cmd.delay_seconds)
    end_at = start_at + timedelta(seconds=cmd.duration_seconds)

    payload = {
        "command": "publish",
        "alert_no": cmd.alert_no,
        "regions": list(cmd.regions),
        "level": cmd.level,
        "message": cmd.message,
        "delay_seconds": cmd.delay_seconds,
        "duration_seconds": cmd.duration_seconds,
    }
    receipt = {
        "request_no": cmd.request_no,
        "command": "publish",
        "status": "accepted",
        "accepted_at": _iso(accepted_at),
        "alert": {
            "alert_no": cmd.alert_no,
            "level": cmd.level,
            "message": cmd.message,
            "regions": list(cmd.regions),
            "delay_seconds": cmd.delay_seconds,
            "duration_seconds": cmd.duration_seconds,
            "accepted_at": _iso(accepted_at),
            "start_at": _iso(start_at),
            "end_at": _iso(end_at),
            "revoked_at": None,
        },
    }

    try:
        async with session.begin():
            inserted = await _try_insert_command(
                session,
                request_no=cmd.request_no,
                command_type="publish",
                payload=payload,
                accepted_at=accepted_at,
            )
            if not inserted:
                return await _replay_or_conflict(
                    session, request_no=cmd.request_no, payload=payload
                )

            taken = await session.scalar(
                select(Alert.alert_no).where(Alert.alert_no == cmd.alert_no)
            )
            if taken is not None:
                raise ApiError(
                    409, "alert_no_conflict", f"告警号 {cmd.alert_no} 已被使用，不可复用"
                )

            session.add(
                Alert(
                    alert_no=cmd.alert_no,
                    level=cmd.level,
                    message=cmd.message,
                    delay_seconds=cmd.delay_seconds,
                    duration_seconds=cmd.duration_seconds,
                    accepted_at=accepted_at,
                    start_at=start_at,
                    end_at=end_at,
                    revoked_at=None,
                    regions=[AlertRegion(region=r) for r in cmd.regions],
                )
            )
            await session.flush()
            await _store_receipt(session, request_no=cmd.request_no, receipt=receipt)
    except IntegrityError:
        # 并发下不同请求号争抢同一告警号：主键冲突，整体回滚，不留部分状态
        raise ApiError(
            409, "alert_no_conflict", f"告警号 {cmd.alert_no} 已被使用，不可复用"
        ) from None
    return receipt


async def execute_revoke(session: AsyncSession, cmd: RevokeCommand) -> dict:
    """撤销告警：按受理时间终止其后续播发，历史时刻仍可查询。"""
    accepted_at = datetime.now(timezone.utc)
    payload = {"command": "revoke", "alert_no": cmd.alert_no}

    async with session.begin():
        inserted = await _try_insert_command(
            session,
            request_no=cmd.request_no,
            command_type="revoke",
            payload=payload,
            accepted_at=accepted_at,
        )
        if not inserted:
            return await _replay_or_conflict(
                session, request_no=cmd.request_no, payload=payload
            )

        # revoked_at 只许提前不许推迟（LEAST），并发撤销时保留最早的终止时刻
        stmt = (
            update(Alert)
            .where(Alert.alert_no == cmd.alert_no)
            .values(
                revoked_at=func.least(
                    func.coalesce(Alert.revoked_at, accepted_at), accepted_at
                )
            )
            .returning(Alert.revoked_at)
        )
        revoked_at = (await session.execute(stmt)).scalar_one_or_none()
        if revoked_at is None:
            raise ApiError(404, "alert_not_found", f"告警号 {cmd.alert_no} 不存在，无法撤销")

        receipt = {
            "request_no": cmd.request_no,
            "command": "revoke",
            "status": "accepted",
            "accepted_at": _iso(accepted_at),
            "alert_no": cmd.alert_no,
            "revoked_at": _iso(revoked_at),
        }
        await _store_receipt(session, request_no=cmd.request_no, receipt=receipt)
    return receipt


async def get_alert_view(session: AsyncSession, alert_no: int) -> dict:
    alert = await session.scalar(select(Alert).where(Alert.alert_no == alert_no))
    if alert is None:
        raise ApiError(404, "alert_not_found", f"告警号 {alert_no} 不存在")
    return alert_view(alert)


def _active_conditions(at: datetime) -> list:
    """时刻 at 处于生效区间 [start_at, end_at) 且未被撤销（撤销时刻起终止）。"""
    return [
        Alert.start_at <= at,
        Alert.end_at > at,
        or_(Alert.revoked_at.is_(None), Alert.revoked_at > at),
    ]


# 同区域多条同时有效时的裁决顺序：级别高 -> 受理时间早 -> 告警号小
_ARBITRATION_ORDER = (Alert.level.desc(), Alert.accepted_at.asc(), Alert.alert_no.asc())


async def query_broadcasts(
    session: AsyncSession, at: datetime, region: str | None
) -> dict:
    if region is not None:
        stmt = (
            select(Alert)
            .join(AlertRegion, AlertRegion.alert_no == Alert.alert_no)
            .where(AlertRegion.region == region)
            .where(*_active_conditions(at))
            .order_by(*_ARBITRATION_ORDER)
            .limit(1)
        )
        alert = (await session.scalars(stmt)).first()
        return {
            "at": _iso(at),
            "region": region,
            "alert": alert_view(alert) if alert else None,
        }

    # 全部区域：每个区域按同一裁决顺序取一条（DISTINCT ON）
    stmt = (
        select(AlertRegion.region, Alert)
        .join(Alert, Alert.alert_no == AlertRegion.alert_no)
        .where(*_active_conditions(at))
        .order_by(AlertRegion.region.asc(), *_ARBITRATION_ORDER)
        .distinct(AlertRegion.region)
    )
    rows = (await session.execute(stmt)).all()
    return {
        "at": _iso(at),
        "broadcasts": [{"region": r, "alert": alert_view(a)} for r, a in rows],
    }
