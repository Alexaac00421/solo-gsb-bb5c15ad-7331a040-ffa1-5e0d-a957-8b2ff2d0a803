"""业务逻辑：命令受理（发布/撤销）与时刻裁决查询。

并发与幂等约定:
- command_records.request_no 为主键；首次受理用 INSERT ... ON CONFLICT DO NOTHING 抢占，
  并发/串行重试读取首次回执；载荷哈希不同直接 409。
- 发布告警号全局唯一且不可复用；同号并发由 SELECT ... FOR UPDATE 串行化裁决。
- 撤销只提前逻辑终止时间（revoked_at），不改写原始计划区间，历史时刻照常可查。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import Alert, AlertArea, CommandRecord
from .schemas import (
    BroadcastItem,
    BroadcastQueryResult,
    PublishCommand,
    PublishReceipt,
    RevokeCommand,
    RevokeReceipt,
)


class ServiceError(Exception):
    """业务错误基类，由 API 层映射为 HTTP 状态码。"""

    code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConflictError(ServiceError):
    code = "conflict"


class NotFoundError(ServiceError):
    code = "target_not_found"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_at(value: datetime | None) -> datetime:
    """查询时刻归一化：无时区信息一律按 UTC 处理。"""
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _payload_hash(command_type: str, payload: dict[str, Any]) -> str:
    # 键排序保证序列化稳定
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(f"{command_type}|{canonical}".encode("utf-8")).hexdigest()


def publish_payload_hash(cmd: PublishCommand) -> str:
    return _payload_hash(
        "publish",
        {
            "alert_no": cmd.alert_no,
            "areas": sorted(cmd.areas),  # 区域顺序不影响同一性
            "level": cmd.level,
            "content": cmd.content,
            "delay_seconds": cmd.delay_seconds,
            "duration_seconds": cmd.duration_seconds,
        },
    )


def revoke_payload_hash(cmd: RevokeCommand) -> str:
    return _payload_hash(
        "revoke",
        {"alert_no": cmd.alert_no, "reason": cmd.reason},
    )


def accept_publish(
    session: Session, cmd: PublishCommand, now: datetime
) -> tuple[PublishReceipt, bool]:
    """受理发布命令。返回 (回执, 是否首次受理)。失败整体回滚，不留部分状态。"""
    payload_hash = publish_payload_hash(cmd)
    record = _claim_request(
        session,
        request_no=cmd.request_no,
        command_type="publish",
        payload_hash=payload_hash,
        accepted_at=now,
    )
    if record is not None:
        return PublishReceipt.model_validate_json(record.receipt_json), False

    # 新请求号：占用告警号（行锁串行化同号并发发布）
    existing = session.scalar(
        select(Alert).where(Alert.alert_no == cmd.alert_no).with_for_update()
    )
    if existing is not None:
        raise ConflictError(f"告警号 {cmd.alert_no} 已存在且不可复用")

    effective_at = now + timedelta(seconds=cmd.delay_seconds)
    expire_at = effective_at + timedelta(seconds=cmd.duration_seconds)

    alert = Alert(
        alert_no=cmd.alert_no,
        level=cmd.level,
        content=cmd.content,
        delay_seconds=cmd.delay_seconds,
        duration_seconds=cmd.duration_seconds,
        published_at=now,
        effective_at=effective_at,
        expire_at=expire_at,
    )
    # 区域作为同一张事务里的子记录：要么全部落库，要么整体回滚
    alert.areas = [AlertArea(area=area) for area in cmd.areas]
    session.add(alert)

    receipt = PublishReceipt(
        request_no=cmd.request_no,
        alert_no=cmd.alert_no,
        areas=list(cmd.areas),
        level=cmd.level,
        content=cmd.content,
        delay_seconds=cmd.delay_seconds,
        duration_seconds=cmd.duration_seconds,
        accepted_at=now,
        effective_at=effective_at,
        expire_at=expire_at,
    )
    _store_receipt(session, cmd.request_no, receipt.model_dump_json())
    return receipt, True


def accept_revoke(
    session: Session, cmd: RevokeCommand, now: datetime
) -> tuple[RevokeReceipt, bool]:
    """受理撤销命令。返回 (回执, 是否首次受理)。"""
    payload_hash = revoke_payload_hash(cmd)
    record = _claim_request(
        session,
        request_no=cmd.request_no,
        command_type="revoke",
        payload_hash=payload_hash,
        accepted_at=now,
    )
    if record is not None:
        return RevokeReceipt.model_validate_json(record.receipt_json), False

    alert = session.scalar(
        select(Alert).where(Alert.alert_no == cmd.alert_no).with_for_update()
    )
    if alert is None:
        raise NotFoundError(f"撤销目标告警 {cmd.alert_no} 不存在")
    if alert.revoked_at is not None:
        raise ConflictError(f"告警 {cmd.alert_no} 已被撤销")
    if now >= alert.expire_at:
        raise ConflictError(f"告警 {cmd.alert_no} 已自然到期，无需撤销")

    # 撤销受理时间即终止时刻；若早于延迟生效点，该告警从不播发（历史区间仍保留）
    alert.revoked_at = now

    receipt = RevokeReceipt(
        request_no=cmd.request_no,
        alert_no=cmd.alert_no,
        reason=cmd.reason,
        accepted_at=now,
        effective_at=alert.effective_at,
        stop_at=now,
    )
    _store_receipt(session, cmd.request_no, receipt.model_dump_json())
    return receipt, True


def _claim_request(
    session: Session,
    *,
    request_no: str,
    command_type: str,
    payload_hash: str,
    accepted_at: datetime,
) -> CommandRecord | None:
    """抢占请求号。返回已有记录表示重试；返回 None 表示首次受理。

    已有记录但载荷哈希不同 → 409（请求号复用且命令不一致）。
    并发对端若回滚（行消失）则有限次重试抢占。
    """
    stmt = (
        pg_insert(CommandRecord)
        .values(
            request_no=request_no,
            command_type=command_type,
            payload_hash=payload_hash,
            accepted_at=accepted_at,
            receipt_json="",
        )
        .on_conflict_do_nothing(index_elements=["request_no"])
        # psycopg3 下无 RETURNING 的 INSERT rowcount 恒为 -1，
        # 用 RETURNING 判定本次是否真正插入
        .returning(CommandRecord.request_no)
    )
    for _ in range(10):
        inserted = session.execute(stmt).first()
        session.flush()
        if inserted is not None:
            return None

        # 并发的另一个事务可能尚未提交；FOR UPDATE 等待其结束后再读首次回执
        existing = session.scalar(
            select(CommandRecord)
            .where(CommandRecord.request_no == request_no)
            .with_for_update()
        )
        if existing is None:
            # 占位行所属的并发事务回滚了，重试抢占
            continue
        if existing.payload_hash != payload_hash:
            raise ConflictError(
                f"请求号 {request_no} 已用于另一条命令，载荷不一致，拒绝复用"
            )
        return existing
    raise ConflictError("请求号处理冲突，请稍后重试")  # pragma: no cover


def _store_receipt(session: Session, request_no: str, receipt_json: str) -> None:
    """把首次回执回填到幂等记录（记录已由 _claim_request 插入并持锁）。"""
    session.execute(
        update(CommandRecord)
        .where(CommandRecord.request_no == request_no)
        .values(receipt_json=receipt_json)
    )


def query_broadcasts(
    session: Session, at: datetime, areas: list[str] | None = None
) -> BroadcastQueryResult:
    """裁决指定时刻各区域应播发的告警。

    同一区域多条同时有效时取：级别高 → 受理时间早 → 告警号小。
    用 DISTINCT ON (area) 一条 SQL 完成；高优先级到期后低优先级自动恢复，
    因为结果完全由查询时刻推导，不依赖任何运行期状态翻转。
    """
    # 实际播发结束时间 = 自然到期与撤销时刻的较早者
    effective_end = func.least(
        Alert.expire_at, func.coalesce(Alert.revoked_at, Alert.expire_at)
    )

    stmt = (
        select(
            AlertArea.area,
            Alert.alert_no,
            Alert.level,
            Alert.content,
            Alert.effective_at,
            effective_end.label("expire_at"),
            Alert.published_at,
        )
        .join(Alert, Alert.alert_no == AlertArea.alert_no)
        .where(Alert.effective_at <= at, at < effective_end)
        .order_by(
            AlertArea.area,
            Alert.level.desc(),
            Alert.published_at.asc(),
            Alert.alert_no.asc(),
        )
        .distinct(AlertArea.area)
    )
    if areas:
        stmt = stmt.where(AlertArea.area.in_(sorted(set(areas))))

    rows = session.execute(stmt).all()
    return BroadcastQueryResult(
        at=at,
        broadcasts=[
            BroadcastItem(
                area=row.area,
                alert_no=row.alert_no,
                level=row.level,
                content=row.content,
                effective_at=row.effective_at,
                expire_at=row.expire_at,
                published_at=row.published_at,
            )
            for row in rows
        ],
    )
