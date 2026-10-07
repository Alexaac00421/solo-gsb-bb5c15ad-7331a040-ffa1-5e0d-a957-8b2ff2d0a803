"""运行期配置，全部来自环境变量，均带默认值，便于容器一键启动。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _database_url() -> str:
    # Compose 中直接注入 DATABASE_URL；本地裸跑时用分项变量拼装。
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "broadcast")
    password = os.getenv("POSTGRES_PASSWORD", "broadcast")
    db = os.getenv("POSTGRES_DB", "broadcast")
    host = os.getenv("POSTGRES_HOST", "db")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


@dataclass(frozen=True)
class Settings:
    database_url: str = _database_url()
    # 服务监听地址/端口（uvicorn 在入口中读取）
    host: str = os.getenv("APP_HOST", "0.0.0.0")
    port: int = int(os.getenv("APP_PORT", "8080"))
    # 延迟/持续秒数上限：约 10 年，防止越界与异常调度
    max_seconds: int = int(os.getenv("MAX_SECONDS", str(10 * 365 * 24 * 3600)))


settings = Settings()
