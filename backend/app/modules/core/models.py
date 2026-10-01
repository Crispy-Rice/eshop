"""core 模块的 ORM 模型。

本地消息表（outbox）是最终一致性的基础设施，字段说明见
docs/13-schema.md §4。这里只定义模型，写入与投递逻辑在 service.py。

注意：这里的 comment 必须与 baseline 迁移里的写法**逐字一致**，
否则 autogenerate 会为"注释不一致"生成一堆无意义的 alter_column。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base


class LocalMessage(Base):
    """业务事务内落库、由投递协程异步推到 Redis Streams 的消息。

    保证"业务提交成功 ⇒ 消息一定存在"，再配合消费端按 biz_key 幂等，
    就得到了至少一次的可靠投递，不需要引入独立 MQ。
    """

    __tablename__ = "local_message"
    __table_args__ = (
        # 同一事件不会被写入两次
        UniqueConstraint("topic", "biz_key"),
        # 部分索引：只包含未完成的消息，体积始终很小
        Index("idx_local_message_pending", "next_retry_at", postgresql_where=text("status IN (0, 2)")),
        {"schema": "core", "comment": "本地消息表（事务消息的替代）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    topic: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="事件主题，对应 Redis Stream stream:{topic}"
    )
    biz_key: Mapped[str] = mapped_column(String(160), nullable=False, comment="幂等键，消费者据此去重")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("0"),
        comment="0待发送 1已发送 2发送失败(待重试) 3已放弃",
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[datetime] = mapped_column(
        TS, nullable=False, server_default=func.now(), comment="下次重试时间（指数退避）"
    )
    error_msg: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
