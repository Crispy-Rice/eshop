"""freight 模块的请求/响应模型。"""

from __future__ import annotations

from pydantic import Field

from app.core.schemas import CamelModel, Quantity, SnowflakeId


# ============================================================
# 模板
# ============================================================
class FreightTemplateCreateRequest(CamelModel):
    name: str = Field(min_length=1, max_length=64)
    charge_type: int = Field(default=1, ge=1, le=3, description="1按重量 2按件数 3按体积")
    first_unit: int = Field(default=1000, ge=0, description="首重（克）。按件数时表示首件数")
    first_price: int = Field(ge=0, description="首重价（分）")
    add_unit: int = Field(default=1000, gt=0, description="续重单位（克）")
    add_price: int = Field(ge=0, description="续重价（分/单位）")
    free_shipping: bool = Field(default=False, description="全场包邮")
    free_threshold: int = Field(default=0, ge=0, description="满额包邮（分），0=不参与")
    free_num: int = Field(default=0, ge=0, description="满件包邮，0=不参与")
    merge_type: int = Field(default=1, ge=1, le=2, description="1同仓取首重最高 2各算各的")


class FreightTemplateUpdateRequest(FreightTemplateCreateRequest):
    status: int = Field(default=1, ge=1, le=2, description="1启用 2停用")


class FreightTemplateOut(CamelModel):
    id: SnowflakeId
    name: str
    charge_type: int
    first_unit: int
    first_price: int
    add_unit: int
    add_price: int
    free_shipping: bool
    free_threshold: int
    free_num: int
    merge_type: int
    status: int
    # 影响面：改这个模板会影响多少个 SKU
    bound_sku_count: int = 0


class FreightTemplateUpdateOut(CamelModel):
    template: FreightTemplateOut
    affected_sku_count: int
    notice: str


# ============================================================
# 区域规则
# ============================================================
class RegionRuleIn(CamelModel):
    region_code: str = Field(max_length=16, description='"0" 表示全国默认')
    region_level: int = Field(default=1, ge=1, le=3, description="1省 2市 3区")
    first_unit: int = Field(ge=0)
    first_price: int = Field(ge=0)
    add_unit: int = Field(gt=0)
    add_price: int = Field(ge=0)
    free_shipping: bool = False
    enabled: bool = True
    priority: int = 0


class RegionRuleOut(RegionRuleIn):
    id: SnowflakeId


class RegionRulesReplaceRequest(CamelModel):
    """整体替换（不做逐条 diff）。"""

    rules: list[RegionRuleIn] = Field(default_factory=list, max_length=200)


class ExcludeRegionIn(CamelModel):
    region_code: str = Field(max_length=16)
    reason: str | None = Field(default=None, max_length=64)


class ExcludeRegionOut(ExcludeRegionIn):
    id: SnowflakeId


class ExcludeRegionsReplaceRequest(CamelModel):
    excludes: list[ExcludeRegionIn] = Field(default_factory=list, max_length=200)


# ============================================================
# SKU 绑定
# ============================================================
class SkuBindRequest(CamelModel):
    sku_id: SnowflakeId
    template_id: SnowflakeId
    warehouse_id: SnowflakeId
    priority: int = Field(default=0, description="同 SKU 多模板时越大越优先")


class SkuBindOut(CamelModel):
    """一条 SKU↔模板 的绑定关系。

    带上商品标题与规格，界面不用为了显示名字再跑一趟 —— ``batch_get_skus``
    在本模块的 ``estimate_freight`` 里已经这么用了，``freight → product``
    是既有依赖（docs/01 §2 的依赖图没画全）。
    """

    sku_id: SnowflakeId
    sku_code: str
    spu_title: str
    spec_text: str
    warehouse_id: SnowflakeId
    warehouse_name: str
    priority: int
    enabled: bool


# ============================================================
# 买家：估运费
# ============================================================
class FreightEstimateItemIn(CamelModel):
    sku_id: SnowflakeId
    num: Quantity = 1


class FreightEstimateRequest(CamelModel):
    items: list[FreightEstimateItemIn] = Field(min_length=1, max_length=100)
    address_id: SnowflakeId


class FreightPackageOut(CamelModel):
    warehouse_id: SnowflakeId
    template_name: str
    freight: int
    weight_g: int
    qty: int
    is_free: bool
    free_reason: str | None = None


class FreightEstimateOut(CamelModel):
    total: int
    packages: list[FreightPackageOut]
    notices: list[str] = Field(default_factory=list)
