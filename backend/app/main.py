"""FastAPI 应用入口。

启动顺序（lifespan）：
    日志 → 雪花 worker id 租约（依赖 Redis）→ 注册路由
关闭时逆序释放连接。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings
from app.core.context import request_id_var
from app.core.db import dispose_engine, get_session_factory
from app.core.errors import BizError, CachedResponse, ErrorCode
from app.core.logging import setup_logging
from app.core.redis import close_redis, get_redis, ping_redis
from app.core.response import ApiResponse
from app.core.snowflake import start_snowflake, stop_snowflake
from app.router import register_routers

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info(
        "服务启动中",
        extra={"env": settings.app_env, "version": settings.app_version},
    )

    # 雪花 worker id 必须从 Redis 租。这里失败就直接不启动 ——
    # 容器编排会重启，比起用重复的 worker id 发出重复 ID，宁可暂时不可用。
    await start_snowflake(get_redis())

    logger.info("服务已就绪")
    try:
        yield
    finally:
        logger.info("服务关闭中")
        await stop_snowflake()
        await close_redis()
        await dispose_engine()
        logger.info("服务已关闭")


settings = get_settings()

app = FastAPI(
    title="eshop API",
    version=settings.app_version,
    lifespan=lifespan,
    # 生产环境不暴露接口文档（docs/16-deployment.md §9 第 4 条）
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)


# ============================================================
# 中间件：请求 ID
# ============================================================
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):  # noqa: ANN001, ANN201
    """Nginx 会生成 X-Request-Id 并透传；没有就自己生成一个。

    写进 ContextVar，日志和错误响应都会自动带上，方便用户报障时定位。
    """
    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    token = request_id_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-Id"] = request_id
    return response


# ============================================================
# 全局异常处理
# ============================================================
_HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.SYSTEM_BUSY,
}


@app.exception_handler(BizError)
async def biz_error_handler(request: Request, exc: BizError) -> JSONResponse:
    """可预期的业务异常：不打堆栈，按错误码定义的状态码返回。"""
    return JSONResponse(
        status_code=exc.http_status,
        content=ApiResponse.fail(exc.code.value, exc.message, exc.data).model_dump(by_alias=True),
    )


@app.exception_handler(CachedResponse)
async def cached_response_handler(request: Request, exc: CachedResponse) -> JSONResponse:
    """幂等命中：把上次的响应体原样返回（docs/10-idempotency.md §3.3）。"""
    return JSONResponse(status_code=status.HTTP_200_OK, content=exc.body)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """请求体/参数校验失败。

    FastAPI 默认返回 422，本系统约定 422 专用于"业务规则不满足"，
    参数错误统一为 400（docs/15-api-and-errors.md §3.1）。
    """
    errors = [
        {
            "field": ".".join(str(p) for p in err.get("loc", ()) if p != "body"),
            "reason": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ApiResponse.fail(
            ErrorCode.VALIDATION_ERROR.value, ErrorCode.VALIDATION_ERROR.default_message, {"errors": errors}
        ).model_dump(by_alias=True),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把框架自身的 404/405 等也包成统一响应格式。

    否则未知路径会返回 FastAPI 默认的 ``{"detail": "Not Found"}``，
    与其它接口的格式不一致，前端得写两套解析。
    """
    code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.VALIDATION_ERROR)
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(code.value, code.default_message).model_dump(by_alias=True),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底：记录完整堆栈，但只把 requestId 返回给用户。

    ★ 绝不把异常信息、SQL 错误、表名字段名泄露给外部（docs/15 §3.3）。
    """
    logger.exception("未处理的异常", extra={"path": request.url.path, "method": request.method})
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ApiResponse.fail(
            ErrorCode.INTERNAL_ERROR.value, ErrorCode.INTERNAL_ERROR.default_message
        ).model_dump(by_alias=True),
    )


# ============================================================
# 健康检查
# ============================================================
@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """存活探针：只说明进程还在，不检查依赖（依赖挂了也不该重启进程）。"""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """就绪探针：真正探测 PostgreSQL 与 Redis。

    任一不可用时返回 503，让负载均衡把流量摘走。
    """
    checks: dict[str, str] = {}

    try:
        async with get_session_factory()() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - 探针要报告而不抛
        logger.warning("readyz: PostgreSQL 不可用", extra={"error": str(exc)})
        checks["postgres"] = "error"

    checks["redis"] = "ok" if await ping_redis() else "error"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )


register_routers(app)
