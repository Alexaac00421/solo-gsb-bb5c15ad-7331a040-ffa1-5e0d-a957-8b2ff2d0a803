"""SQLAlchemy 模型与建表 DDL。

表结构:
- command_records: 请求号幂等表，保存首次受理的命令与回执
- alerts:         告警主表，生效区间为半开区间 [effective_at, expire_at)
- alert_areas:    告警-区域 多对多关联，同一区域在同一时刻仅一条告警胜出
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CommandRecord(Base):
    __tablename__ = "command_records"

    # 值守员提供的请求号（字符串化后存储），全局唯一
    request_no: Mapped[str] = mapped_column(String(128), primary_key=True)
    # publish | revoke
    command_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # 规范化载荷的 SHA-256，用于重试一致性校验
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 首次受理时间（UTC，时区感知）
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 首次回执原文（JSON 文本），重试原样返回
    receipt_json: Mapped[str] = mapped_column(Text, nullable=False)


class Alert(Base):
    __tablename__ = "alerts"

    # 业务告警号，不可复用
    alert_no: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    delay_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 发布命令的受理时间
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 生效起止（半开区间）；撤销时把 expire_at 提前为撤销受理时间
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expire_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 撤销命令的受理时间；未撤销为 NULL
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    areas: Mapped[list["AlertArea"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("level BETWEEN 1 AND 5", name="ck_alert_level"),
        CheckConstraint("delay_seconds >= 0", name="ck_alert_delay_nonneg"),
        CheckConstraint("duration_seconds > 0", name="ck_alert_duration_pos"),
        CheckConstraint("effective_at < expire_at", name="ck_alert_interval"),
    )


class AlertArea(Base):
    __tablename__ = "alert_areas"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alert_no: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("alerts.alert_no", ondelete="CASCADE"),
        nullable=False,
    )
    area: Mapped[str] = mapped_column(String(256), nullable=False)

    alert: Mapped[Alert] = relationship(back_populates="areas")

    __table_args__ = (
        UniqueConstraint("alert_no", "area", name="uq_alert_area"),
        # 时刻裁决查询走 (area) 过滤；告警号上随外键已有索引
        Index("ix_alert_areas_area", "area"),
    )
