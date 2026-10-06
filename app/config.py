from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """服务配置，均可通过环境变量覆盖。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL 连接串（asyncpg 驱动）
    database_url: str = Field(
        default="postgresql+asyncpg://broadcast:broadcast@localhost:5432/broadcast",
        validation_alias="DATABASE_URL",
    )
    # 启动时等待数据库就绪的重试次数与间隔（秒）
    db_connect_retries: int = Field(default=30, validation_alias="DB_CONNECT_RETRIES")
    db_connect_retry_interval: float = Field(
        default=1.0, validation_alias="DB_CONNECT_RETRY_INTERVAL"
    )


settings = Settings()
