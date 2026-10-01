"""cart 模块的 ORM 模型。

对应 docs/02-domain-model.md §4。这是一张**刻意做轻**的表：

- **只存 PG，不进 Redis**（docs/14 §2.5）——数据量与并发都不需要缓存层
- **不存库存可售量**——购物车只存用户意图，库存校验在结算页做；
  列表里的"有效/无货/下架"是每次实时查出来的，不入库
- **没有幂等键表、没有流水**——购物车是可丢弃的用户意图，
  不值得 inventory 那套基础设施

真正的约束只有一条：``UNIQUE (user_id, sku_id)`` —— 同一 SKU 只能有一行，
重复加购走 ``num = num + N``。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    SmallInteger,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# 加购来源
CART_SOURCE_DETAIL = 1  # 详情页
CART_SOURCE_LIST = 2  # 列表页
CART_SOURCE_PROMO = 3  # 活动页

# 单 SKU 数量上限。与 docs/02 §4 要点 3 一致，SQL 里的 LEAST(..., 200) 是同一常量
MAX_NUM_PER_SKU = 200
# 整车 SKU 种类上限（防刷）
MAX_SKUS_PER_CART = 100


class CartItem(Base):
    """购物车项，粒度 = **SKU**（不是 SPU）。

    用户在详情页切换规格后加购，加的是当前选中的那个 SKU ——
    "黑色 256G"和"白色 512G"是两行。
    """

    __tablename__ = "cart_item"
    __table_args__ = (
        # ★ 同一用户 + 同一 SKU 只能有一行。加购是累加 num，不是插新行
        UniqueConstraint("user_id", "sku_id", name="uk_cart_user_sku"),
        # 列表按店铺分组展示，这是主查询路径
        Index("idx_cart_user_shop", "user_id", "shop_id"),
        CheckConstraint(f"num BETWEEN 1 AND {MAX_NUM_PER_SKU}", name="num_range"),
        {"schema": "cart"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 冗余：按店铺分组展示要用，省一次关联
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="冗余，便于按店铺分组")
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    spu_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="冗余，便于批量查 SPU")

    price_snapshot: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="加购时价格（分）。只用于降价提醒，**不参与结算**",
    )
    num: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    selected: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("true"), comment="是否勾选，结算只算勾选的"
    )
    source: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("1"),
        comment="1详情页 2列表页 3活动页",
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
