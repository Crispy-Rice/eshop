"""结构化日志。

输出 JSON 到 stdout（docker 直接收），字段固定，便于后续采集到日志系统。
自动带上当前请求的 ``requestId``（来自 X-Request-Id，由 Nginx 生成）。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from app.core.context import current_request_id

# LogRecord 自带的属性，不算业务附加字段
_RESERVED: frozenset[str] = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        request_id = current_request_id()
        if request_id:
            payload["requestId"] = request_id

        # 通过 logger.info("...", extra={"orderNo": ...}) 传入的字段
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # uvicorn 默认自己装 handler，输出格式不统一；全部交给 root
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
        lg.setLevel(level)

    # 这些库太吵
    logging.getLogger("sqlalchemy.engine").setLevel("WARNING")
    logging.getLogger("asyncio").setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
