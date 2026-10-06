from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import settings
from .db import engine
from .errors import ApiError
from .models import Base
from .routes import router

logger = logging.getLogger("broadcast")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时等待数据库就绪并初始化表结构，退出时释放连接池。"""
    last_exc: Exception | None = None
    for attempt in range(1, settings.db_connect_retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("database ready, schema ensured")
            break
        except Exception as exc:  # noqa: BLE001 - 启动期等待依赖就绪
            last_exc = exc
            logger.warning(
                "waiting for database (%d/%d): %s",
                attempt,
                settings.db_connect_retries,
                exc,
            )
            await asyncio.sleep(settings.db_connect_retry_interval)
    else:
        raise RuntimeError(f"database not ready: {last_exc}")
    yield
    await engine.dispose()


app = FastAPI(
    title="园区应急广播播发裁决服务",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "请求参数校验失败",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


app.include_router(router)
