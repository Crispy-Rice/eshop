"""product 模块的 ORM 模型。

对应 docs/02-domain-model.md 与 docs/13-schema.md。三条核心约定：

1. **购物车、订单、库存、价格、运费全部挂在 SKU 上**，SPU 只负责展示聚合。
   SPU 上的 price_min/price_max 是给列表页看的展示区间，**永远不能用于下单**。
2. **库存不在 sku 表里**，而在 inventory.sku_stock + Redis。
3. **规格组合由 spec_group / spec_value / sku_spec 三张表表达**：
   `sku_spec` 的主键 (sku_id, spec_group_id) 保证一个 SKU 在每个规格组下
   只能取一个值，从数据库层面挡住"一个 SKU 同时是黑色和白色"这类脏数据。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# SPU 状态。4 是平台强制下架的违规商品，与审核无关。
SPU_DRAFT = 1
SPU_ON_SHELF = 2
SPU_OFF_SHELF = 3
SPU_BANNED = 4
SPU_PENDING_AUDIT = 5
# 平台驳回：商家按审核意见改完可以再提交。
# ★ 以前驳回是复用 SPU_DRAFT 的，于是"从没提交过"和"打回来待改"在商家列表里
#   长得一模一样 —— 商家分不清哪些是自己没写完的、哪些是等着他动手改的。
SPU_REJECTED = 6

# 商家可以提交审核的状态：新建的草稿，和被打回待修改的。
# ★ 用具名集合而不是散落的 ``status in (1, 6)``：以后再加一个"可提交"的状态时，
#   只改这里，不会漏掉某个分支。
SPU_SUBMITTABLE = (SPU_DRAFT, SPU_REJECTED)

# 还没通过平台审核的状态 —— 不允许直接上架，必须先走审核。
SPU_NOT_APPROVED = (SPU_DRAFT, SPU_PENDING_AUDIT, SPU_REJECTED)

# SKU 状态：只跟随 SPU，单独下架某个 SKU 用不到（下架 SPU 即可）
SKU_OFF_SHELF = 2


class Category(Base):
    """类目树（最多三级）。

    ``path`` 是物化路径（如 ``/1/10/101/``），查"某类目下的所有商品"时
    用 ``path LIKE '/1/10/%'`` 一次拿到整棵子树，不用递归查询。
    """

    __tablename__ = "category"
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uk_category_parent_name"),
        Index("idx_category_path", "path"),
        Index("idx_category_status_sort", "status", "sort"),
        CheckConstraint("level BETWEEN 1 AND 3", name="level_valid"),
        {"schema": "product"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(BigInteger, comment="NULL 表示一级类目")
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="1/2/3 级")
    path: Mapped[str] = mapped_column(String(128), nullable=False, comment="物化路径，形如 /1/10/101/")
    sort: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1启用 2停用"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class Spu(Base):
    """标准产品单元（抽象商品）。搜索、浏览、分享、评价都挂在这一层。"""

    __tablename__ = "spu"
    __table_args__ = (
        Index("idx_spu_shop_status", "shop_id", "status", "created_at"),
        Index("idx_spu_category_status", "category_id", "status"),
        # 列表页按类目浏览时用
        Index(
            "idx_spu_on_shelf_category",
            "category_id",
            "sort_weight",
            postgresql_where=text("status = 2 AND NOT deleted"),
        ),
        # 标题/规格的模糊搜索（docs/02 §7）
        Index(
            "idx_spu_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        CheckConstraint("price_max >= price_min AND price_min >= 0", name="price_range_valid"),
        {"schema": "product"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="末级类目 ID")
    brand_id: Mapped[int | None] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    sub_title: Mapped[str | None] = mapped_column(String(255), comment="副标题/卖点")
    main_image: Mapped[str] = mapped_column(String(255), nullable=False, comment="主图，列表页用")
    # 展示价区间：由在售 SKU 算出，**不能用于下单**
    price_min: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="展示最低价（分）"
    )
    price_max: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="展示最高价（分）"
    )
    # 冗余统计，由评价模块在同一事务内更新（另有每日全量重算兜底）
    total_sold: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    review_score_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    good_review_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # ★ 可空：**零评价必须是 NULL**。原来的 `NOT NULL DEFAULT 5.00` 会让没有评价的商品
    #   读出 5.00，前端就显示"5.0 分 / 100% 好评"（docs/12 §9 明令禁止）
    avg_score: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 2), nullable=True, comment="平均分；无评价时为 NULL"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("1"),
        comment="1草稿 2上架 3下架 4违规下架 5待审核 6已驳回",
    )
    # 最近一次审核意见。驳回时写理由、通过时清空 —— 商家要在自己的商品上看到它，
    # 否则商品只是悄悄回到"草稿"，商家不知道改什么（接口一直收 remark，但原先没存）
    audit_remark: Mapped[str | None] = mapped_column(String(255))
    sort_weight: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # 搜索用：标题 + 副标题 + 类目路径 + 所有在售 SKU 的规格值，写入时维护
    search_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # 综合排序分：销量 + 好评率的加权，由 worker 定期重算
    search_rank: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, server_default=text("0"))
    deleted: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false"), comment="软删，物理删除会破坏订单语义"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class Sku(Base):
    """最小售卖单元。价格、重量、封面图都在这一层。"""

    __tablename__ = "sku"
    __table_args__ = (
        # 商家编码在同一商品内唯一，但**已软删的 SKU 不占位** —— 换了规格之后想复用
        # 同一个编码，不该被一条已退役的行挡住。用 partial unique index 表达
        # （先例：inventory/models.py:76 的 uk_warehouse_default）。
        Index(
            "uk_sku_spu_code",
            "spu_id",
            "sku_code",
            unique=True,
            postgresql_where=text("NOT deleted"),
        ),
        Index("idx_sku_spu_status", "spu_id", "status"),
        Index("idx_sku_shop", "shop_id"),
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("weight_g > 0", name="weight_positive"),
        {"schema": "product"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    spu_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("product.spu.id"), nullable=False)
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="冗余，下单/拆单时免 join")
    # 选填。空值一律存 NULL 而不是空串 —— 唯一约束 uk_sku_spu_code 里
    # PostgreSQL 认为每个 NULL 互不相同，所以一个商品下能有多个没编码的 SKU；
    # 存空串的话第二个就会撞约束。
    sku_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="商家编码（选填），同一 SPU 内唯一"
    )
    spec_text: Mapped[str] = mapped_column(
        String(255), nullable=False, comment='规格摘要，如"暗夜黑;256G"，写入订单快照'
    )
    price: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="单价（分）")
    cover_image: Mapped[str] = mapped_column(String(255), nullable=False, comment="封面图，切换规格时展示")
    weight_g: Mapped[int] = mapped_column(Integer, nullable=False, comment="物流重量（克），运费计算用")
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1上架 2下架"
    )
    # 软删。规格被整体替换时，旧 SKU 走这里退役 —— 不物理删除，因为
    # inventory.sku_stock 还在，删了会让那段库存流水从商家列表里消失
    # （list_flows 用 INNER JOIN sku_stock 取 shop_id）。
    # ★ 软删必须**同时**置 status=2：购物车与下单是按 status 过滤的，不看这一列。
    deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="软删标记"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class SpecGroup(Base):
    """规格组，如"颜色"、"容量"。单 SPU 最多 3 组（docs/02 §8）。"""

    __tablename__ = "spec_group"
    __table_args__ = (
        UniqueConstraint("spu_id", "name", name="uk_spec_group_spu_name"),
        Index("idx_spec_group_spu", "spu_id", "sort"),
        {"schema": "product"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    spu_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("product.spu.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class SpecValue(Base):
    """规格值，如"暗夜黑"、"256G"。单组最多 20 个。"""

    __tablename__ = "spec_value"
    __table_args__ = (
        UniqueConstraint("group_id", "value", name="uk_spec_value_group_value"),
        Index("idx_spec_value_group", "group_id", "sort"),
        {"schema": "product"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    group_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("product.spec_group.id"), nullable=False)
    value: Mapped[str] = mapped_column(String(32), nullable=False)
    image: Mapped[str | None] = mapped_column(String(255), comment="色块图，前端渲染规格选择器")
    sort: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class SkuSpec(Base):
    """SKU ↔ 规格值的关联。

    主键 (sku_id, spec_group_id) 保证一个 SKU 在每个规格组下只能有一个取值。
    """

    __tablename__ = "sku_spec"
    __table_args__ = (
        Index("idx_sku_spec_value", "spec_value_id"),
        {"schema": "product"},
    )

    sku_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("product.sku.id"), primary_key=True)
    spec_group_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.spec_group.id"), primary_key=True
    )
    spec_value_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.spec_value.id"), nullable=False
    )
