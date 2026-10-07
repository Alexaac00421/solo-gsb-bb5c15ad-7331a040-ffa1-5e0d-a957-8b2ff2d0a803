"""请求/响应模型与输入校验。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 10 年级上限，与 config.MAX_SECONDS 对齐，仅用于输入校验
_MAX_SECONDS = 10 * 365 * 24 * 3600


def _strict_int(value: object) -> int:
    """拒绝 bool 冒充 int（Python 中 True/False 是 int 的子类）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("必须是整数")
    return value


class PublishCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_no: str = Field(min_length=1, max_length=128, description="请求号（幂等键）")
    alert_no: int = Field(description="不可复用的正整数告警号")
    areas: list[str] = Field(description="非空、互不重复的区域列表")
    level: int = Field(description="告警级别 1~5（5 最高）")
    content: str = Field(description="非空播发文案")
    delay_seconds: int = Field(description="非负整数延迟秒数")
    duration_seconds: int = Field(description="正整数持续秒数")

    @field_validator("request_no", "content")
    @classmethod
    def _nonempty_text(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("不能为空字符串")
        return v

    @field_validator("alert_no")
    @classmethod
    def _alert_no_positive(cls, v: int) -> int:
        _strict_int(v)
        if v <= 0:
            raise ValueError("告警号必须为正整数")
        return v

    @field_validator("areas")
    @classmethod
    def _areas_valid(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("区域列表不能为空")
        cleaned: list[str] = []
        seen: set[str] = set()
        for area in v:
            if not isinstance(area, str) or not area.strip():
                raise ValueError("区域标识必须为非空字符串")
            name = area.strip()
            if name in seen:
                raise ValueError(f"区域重复: {name}")
            seen.add(name)
            cleaned.append(name)
        return cleaned

    @field_validator("level")
    @classmethod
    def _level_range(cls, v: int) -> int:
        _strict_int(v)
        if not 1 <= v <= 5:
            raise ValueError("级别必须在 1~5 之间")
        return v

    @field_validator("delay_seconds")
    @classmethod
    def _delay_nonneg(cls, v: int) -> int:
        _strict_int(v)
        if v < 0:
            raise ValueError("延迟秒数必须为非负整数")
        if v > _MAX_SECONDS:
            raise ValueError("延迟秒数超出允许上限")
        return v

    @field_validator("duration_seconds")
    @classmethod
    def _duration_positive(cls, v: int) -> int:
        _strict_int(v)
        if v <= 0:
            raise ValueError("持续秒数必须为正整数")
        if v > _MAX_SECONDS:
            raise ValueError("持续秒数超出允许上限")
        return v


class RevokeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_no: str = Field(min_length=1, max_length=128)
    alert_no: int = Field(description="要撤销的既有告警号")
    reason: str | None = Field(default=None, description="可选撤销原因")

    @field_validator("request_no")
    @classmethod
    def _request_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("不能为空字符串")
        return v

    @field_validator("alert_no")
    @classmethod
    def _alert_no_int(cls, v: int) -> int:
        _strict_int(v)
        if v <= 0:
            raise ValueError("告警号必须为正整数")
        return v

    @field_validator("reason")
    @classmethod
    def _reason_blank_to_none(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            return None
        return v.strip() if v is not None else None


# ---- 回执与查询响应 ----


class PublishReceipt(BaseModel):
    status: Literal["accepted"] = "accepted"
    command_type: Literal["publish"] = "publish"
    request_no: str
    alert_no: int
    areas: list[str]
    level: int
    content: str
    delay_seconds: int
    duration_seconds: int
    accepted_at: datetime
    effective_at: datetime
    expire_at: datetime


class RevokeReceipt(BaseModel):
    status: Literal["revoked"] = "revoked"
    command_type: Literal["revoke"] = "revoke"
    request_no: str
    alert_no: int
    reason: str | None = None
    accepted_at: datetime
    effective_at: datetime
    stop_at: datetime


class BroadcastItem(BaseModel):
    area: str
    alert_no: int
    level: int
    content: str
    effective_at: datetime
    expire_at: datetime
    published_at: datetime


class BroadcastQueryResult(BaseModel):
    at: datetime
    broadcasts: list[BroadcastItem]
