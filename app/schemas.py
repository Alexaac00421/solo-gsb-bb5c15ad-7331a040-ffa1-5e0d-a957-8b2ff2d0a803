from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

INT32_MAX = 2_147_483_647
INT64_MAX = 9_223_372_036_854_775_807


class _CommandBase(BaseModel):
    """命令公共字段：request_no 为幂等键，拒绝多余字段保证载荷可精确比对。"""

    model_config = ConfigDict(extra="forbid")

    request_no: str = Field(min_length=1, max_length=128)

    @field_validator("request_no", mode="before")
    @classmethod
    def _norm_request_no(cls, v: object) -> object:
        if isinstance(v, bool):
            raise ValueError("request_no 非法")
        if isinstance(v, int):  # 兼容数字形式的请求号
            v = str(v)
        if isinstance(v, str):
            return v.strip()
        return v


class PublishCommand(_CommandBase):
    """发布命令载荷。"""

    alert_no: int = Field(ge=1, le=INT64_MAX)  # 正整数告警号，不可复用
    regions: list[str] = Field(min_length=1, max_length=1000)  # 非空且互不重复
    level: int = Field(ge=1, le=5)
    message: str = Field(max_length=4000)  # 非空文案
    delay_seconds: int = Field(ge=0, le=INT32_MAX)  # 非负整数延迟
    duration_seconds: int = Field(ge=1, le=INT32_MAX)  # 正整数持续时长

    @field_validator("regions")
    @classmethod
    def _norm_regions(cls, v: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in v:
            s = item.strip()
            if not s:
                raise ValueError("区域名称不能为空")
            if len(s) > 128:
                raise ValueError("区域名称过长（最大 128 字符）")
            cleaned.append(s)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("区域列表存在重复项")
        return cleaned

    @field_validator("message")
    @classmethod
    def _norm_message(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("文案不能为空")
        return s


class RevokeCommand(_CommandBase):
    """撤销命令载荷。"""

    alert_no: int = Field(ge=1, le=INT64_MAX)
