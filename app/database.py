"""数据库引擎、会话与建表初始化。"""
from __future__ import annotations

import time

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(retries: int = 30, delay_seconds: float = 1.0) -> None:
    """建表；容器启动时数据库可能尚未就绪，做有限重试。"""
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:  # 连接未就绪等
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError(f"数据库初始化失败: {last_error}")


def wait_for_database(retries: int = 30, delay_seconds: float = 1.0) -> None:
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            last_error = exc
            time.sleep(delay_seconds)
    raise RuntimeError(f"数据库不可用: {last_error}")


def get_session() -> Session:
    """FastAPI 依赖：每请求一个事务，异常回滚。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
