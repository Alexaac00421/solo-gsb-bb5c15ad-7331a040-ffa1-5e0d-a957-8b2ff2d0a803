from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Command(Base):
    """命令流水：以 request_no 为幂等键，记录载荷指纹与首次回执。"""

    __tablename__ = "commands"

    request_no: Mapped[str] = mapped_column(String(128), primary_key=True)
    command_type: Mapped[str] = mapped_column(String(16), nullable=False)  # publish / revoke
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)  # 规范化后的请求载荷
    receipt: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # 首次回执
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )  # 受理时间（服务端时钟）


class Alert(Base):
    """告警：生效区间由受理时间推算 [start_at, end_at)，revoked_at 之后终止播发。"""

    __tablename__ = "alerts"

    alert_no: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # 不可复用
    level: Mapped[int] = mapped_column(Integer, nullable=False)  # 1~5，越大优先级越高
    message: Mapped[str] = mapped_column(Text, nullable=False)
    delay_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    regions: Mapped[list[AlertRegion]] = relationship(
        back_populates="alert", lazy="selectin", cascade="all, delete-orphan"
    )


class AlertRegion(Base):
    """告警覆盖区域：与告警同事务写入，保证多区域同成同败。"""

    __tablename__ = "alert_regions"

    alert_no: Mapped[int] = mapped_column(
        ForeignKey("alerts.alert_no", ondelete="CASCADE"), primary_key=True
    )
    region: Mapped[str] = mapped_column(String(128), primary_key=True)

    alert: Mapped[Alert] = relationship(back_populates="regions")


Index("ix_alert_regions_region", AlertRegion.region)
Index("ix_alerts_window", Alert.start_at, Alert.end_at)
