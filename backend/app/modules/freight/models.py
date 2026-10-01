"""freight 模块的 ORM 模型。

对应 docs/06-freight.md §3。四张表，围绕一个核心设计：

**同一个仓库发出的商品，物理上只是一个包裹，首重只发生一次。**
所以 SKU 可以各自绑定不同的模板（促销期特价运费、备仓用别的模板），
但真正计费时要先按仓库分组、再在组内**裁决出一个模板**（取首重最高的），
用它的规则算一次首重——详见 ``calculator.py``。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    desc,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import TS, Base

# 计费方式
CHARGE_BY_WEIGHT = 1
CHARGE_BY_PIECE = 2
CHARGE_BY_VOLUME = 3  # 第一期未实现，字段保留

CHARGE_TYPE_TEXT: dict[int, str] = {
    CHARGE_BY_WEIGHT: "按重量",
    CHARGE_BY_PIECE: "按件数",
    CHARGE_BY_VOLUME: "按体积",
}

# 多 SKU 合并方式（docs/06 §5）
MERGE_BY_HIGHEST_FIRST_UNIT = 1  # 取首重最高者（默认，也是本期唯一实现的）
MERGE_SEPARATELY = 2  # 各算各的（不合并首重）

# 全国默认区域码。区域规则里必有一条它，否则其他地区无规则可匹配
REGION_ALL = "0"

# 区域层级
REGION_LEVEL_PROVINCE = 1
REGION_LEVEL_CITY = 2
REGION_LEVEL_DISTRICT = 3

# 重量兜底：历史脏数据 weight_g = 0 时按 500g 计（docs/06 §10）
FALLBACK_WEIGHT_G = 500
# 超过这个重量就提示联系客服，避免算出离谱的运费（docs/06 §10）
HUGE_WEIGHT_G = 100_000


class FreightTemplate(Base):
    """运费模板。首重/续重参数 + 三种包邮选项。"""

    __tablename__ = "freight_template"
    __table_args__ = (
        Index("idx_freight_tpl_shop", "shop_id", "status"),
        CheckConstraint("first_price >= 0", name="first_price_non_negative"),
        CheckConstraint("add_price >= 0", name="add_price_non_negative"),
        CheckConstraint("add_unit > 0", name="add_unit_positive"),
        CheckConstraint("first_unit >= 0", name="first_unit_non_negative"),
        {"schema": "freight", "comment": "运费模板"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment='如"默认快递模板"')
    charge_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1按重量 2按件数 3按体积"
    )

    # 首重 / 续重。按件数计费时，first_unit / add_unit 表示"件"
    first_unit: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1000"), comment="首重（克）"
    )
    first_price: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="首重价（分）")
    add_unit: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1000"), comment="续重单位（克）"
    )
    add_price: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="续重价（分/单位）")

    # 包邮选项。★ 判定用的是"该模板下商品"的金额/件数，不是整单金额
    free_shipping: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="全场包邮"
    )
    free_threshold: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="满额包邮（分），0=不参与"
    )
    free_num: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="满件包邮，0=不参与"
    )

    merge_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="同仓多 SKU 的合并方式"
    )
    status: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="1启用 2停用"
    )
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class FreightRegionRule(Base):
    """模板的**区域覆盖**规则。

    每个字段都覆盖模板的默认值，用来实现"不同区域不同首重"。
    区域码按 GB/T 2260 的前缀关系匹配：2 位=省、4 位=市、6 位=区。
    """

    __tablename__ = "freight_region_rule"
    __table_args__ = (
        UniqueConstraint("template_id", "region_code", name="uk_freight_rule_tpl_region"),
        CheckConstraint("add_unit > 0", name="add_unit_positive"),
        CheckConstraint("first_price >= 0 AND add_price >= 0", name="prices_non_negative"),
        {"schema": "freight", "comment": "模板-区域运费规则"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("freight.freight_template.id"), nullable=False
    )
    region_code: Mapped[str] = mapped_column(
        String(16), nullable=False, comment='"0"=全国默认，"999999"=偏远'
    )
    region_level: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="1省 2市 3区"
    )
    first_unit: Mapped[int] = mapped_column(Integer, nullable=False)
    first_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    add_unit: Mapped[int] = mapped_column(Integer, nullable=False)
    add_price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    free_shipping: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="越大越优先（市 > 省 > 全国）"
    )


class SkuFreightBind(Base):
    """SKU 与运费模板的绑定（多对多，带仓库与优先级）。

    "每个 SKU 绑定多个运费规则"的落地：一个 SKU 可以有三条绑定——
    主仓用模板 A（priority 10）、备仓用模板 B（5）、促销期特价模板 C（0）。
    下单时**按实际发货仓筛选**，取 priority 最高的那条生效。
    """

    __tablename__ = "sku_freight_bind"
    __table_args__ = (
        UniqueConstraint("sku_id", "template_id", "warehouse_id", name="uk_sku_freight_bind"),
        # 只索引启用的绑定，按优先级倒序取第一条
        Index(
            "idx_sku_freight_active",
            "sku_id",
            "warehouse_id",
            desc("priority"),
            postgresql_where=text("enabled"),
        ),
        {"schema": "freight", "comment": "SKU 与运费模板绑定（多对多）"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sku_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("freight.freight_template.id"), nullable=False
    )
    warehouse_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="该绑定的适用仓库")
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="同 SKU 多模板时越大越优先"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class FreightExcludeRegion(Base):
    """模板的不发货区域。"""

    __tablename__ = "freight_exclude_region"
    __table_args__ = (
        UniqueConstraint("template_id", "region_code", name="uk_freight_exclude_tpl_region"),
        {"schema": "freight"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("freight.freight_template.id"), nullable=False
    )
    region_code: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), comment='如"暂不配送"')
