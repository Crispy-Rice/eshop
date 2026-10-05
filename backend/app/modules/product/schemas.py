"""product 模块的请求/响应模型。"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator, model_validator

from app.core.schemas import CamelModel, SnowflakeId

# 商品价格必须为正（不是 >= 0）
Price = Annotated[int, Field(gt=0, strict=True, description="价格（分）")]
Weight = Annotated[int, Field(gt=0, strict=True, description="重量（克）")]

# 单 SPU 的规格与 SKU 上限，超了前端规格选择器会卡死（docs/02 §8）
MAX_SPEC_GROUPS = 3
MAX_VALUES_PER_GROUP = 20
MAX_SKUS = 200


# ============================================================
# 类目
# ============================================================
class CategoryCreateRequest(CamelModel):
    parent_id: SnowflakeId | None = None
    name: str = Field(min_length=1, max_length=32)
    sort: int = 0


class CategoryOut(CamelModel):
    id: SnowflakeId
    parent_id: SnowflakeId | None = None
    name: str
    level: int
    sort: int


class CategoryTreeOut(CategoryOut):
    children: list[CategoryTreeOut] = []


CategoryTreeOut.model_rebuild()


class CategoryUpdateRequest(CamelModel):
    """部分更新：只改传了的字段（全量提交会把没动过的字段一起覆盖掉）。"""

    name: str | None = Field(default=None, min_length=1, max_length=32)
    sort: int | None = None
    status: int | None = Field(default=None, ge=1, le=2, description="1 启用 / 2 停用")


class CategoryMoveRequest(CamelModel):
    parent_id: SnowflakeId | None = Field(default=None, description="新上级；null 表示升为一级类目")


class AdminCategoryTreeOut(CamelModel):
    """管理端的类目节点：比公开树多出停用状态和商品数。

    商品数（spu_count）是刻意带上的 —— 运营点删除被挡住时，一眼就能看到是这个原因，
    不用去商品列表里翻。它只算未删除的商品，和删除校验用的是同一个口径。
    """

    id: SnowflakeId
    parent_id: SnowflakeId | None = None
    name: str
    level: int
    sort: int
    status: int
    spu_count: int = 0
    children: list[AdminCategoryTreeOut] = []


AdminCategoryTreeOut.model_rebuild()


# ============================================================
# 商家：发布 / 编辑商品
# ============================================================
class SpecValueIn(CamelModel):
    key: str = Field(min_length=1, max_length=32, description="前端生成的临时标识，SKU 用它引用规格值")
    value: str = Field(min_length=1, max_length=32)
    image: str | None = Field(default=None, max_length=255, description="色块图")


class SpecGroupIn(CamelModel):
    name: str = Field(min_length=1, max_length=32)
    values: list[SpecValueIn] = Field(
        min_length=1, max_length=MAX_VALUES_PER_GROUP, description="同一组内的 key 必须唯一"
    )

    @model_validator(mode="after")
    def _keys_unique(self) -> SpecGroupIn:
        keys = [v.key for v in self.values]
        if len(keys) != len(set(keys)):
            raise ValueError(f"规格组「{self.name}」内的 key 重复")
        values = [v.value for v in self.values]
        if len(values) != len(set(values)):
            raise ValueError(f"规格组「{self.name}」内的取值重复")
        return self


class SkuIn(CamelModel):
    """发布时的单个 SKU。

    ``sku_code`` 是**选填**的：它是给商家自己对接 ERP / 打发货单用的钥匙，
    平台内部一律走雪花 ID —— 没有任何查询按它过滤。没有自建系统的商家
    不该被逼着为每一行编一个号。发布页的「按标题生成编码」按钮还在，
    需要时一键填。
    """

    sku_code: str | None = Field(
        default=None, max_length=64, description="商家编码（选填），填了就要在同一 SPU 内唯一"
    )
    spec_value_keys: list[str] = Field(
        min_length=1, max_length=MAX_SPEC_GROUPS, description="每个规格组各选一个值"
    )
    price: Price
    # 可空：SKU 不单独设图时，展示层回落商品主图（product.service.batch_get_skus）
    cover_image: str = Field(default="", max_length=255)
    weight_g: Weight

    @field_validator("sku_code")
    @classmethod
    def _normalize_sku_code(cls, value: str | None) -> str | None:
        """空串 / 纯空白一律归一成 ``None``。

        ★ 必须是 ``None`` 而不是 ``""``：唯一约束是 ``(spu_id, sku_code)``，
        而 PostgreSQL 认为**每个 NULL 互不相同**，所以一个商品下可以有很多
        个没填编码的 SKU；换成空串，第二个就会撞唯一约束。
        """
        if value is None:
            return None
        return value.strip() or None


class SpuCreateRequest(CamelModel):
    category_id: SnowflakeId
    title: str = Field(min_length=1, max_length=120)
    sub_title: str | None = Field(default=None, max_length=255)
    # 可空：允许"先发布、后补图"。空值由展示层兜底成占位图，
    # 但不能反过来把占位图当真实图片存进来（那会一路传染到购物车和订单快照）
    main_image: str = Field(default="", max_length=255)
    spec_groups: list[SpecGroupIn] = Field(min_length=1, max_length=MAX_SPEC_GROUPS)
    skus: list[SkuIn] = Field(min_length=1, max_length=MAX_SKUS)

    @model_validator(mode="after")
    def _validate_skus(self) -> SpuCreateRequest:
        """校验 SKU 与规格的对应关系。

        允许"无效组合"不存在 —— 比如颜色 {黑,白} × 容量 {256G,512G} 只有
        3 个 SKU，"白色 512G" 不上架是正常的（docs/02 §3）。
        但**不允许**：SKU 少选了规格组、引用了不存在的 key、或者两个 SKU
        指向同一组规格值。
        """
        group_names = [g.name for g in self.spec_groups]
        if len(group_names) != len(set(group_names)):
            raise ValueError("规格组名称重复")

        key_to_group: dict[str, str] = {}
        for group in self.spec_groups:
            for value in group.values:
                if value.key in key_to_group:
                    raise ValueError(f"规格值 key「{value.key}」在多个规格组中重复使用")
                key_to_group[value.key] = group.name

        seen_combos: set[frozenset[str]] = set()
        codes: set[str] = set()
        for index, sku in enumerate(self.skus, start=1):
            # 编码是选填的，报错时拿它当标识；没填就退回行号 —— 否则消息里会出现 "None"
            label = sku.sku_code or f"#{index}"
            if len(sku.spec_value_keys) != len(self.spec_groups):
                raise ValueError(f"SKU「{label}」需要为 {len(self.spec_groups)} 个规格组各选一个值")
            for key in sku.spec_value_keys:
                if key not in key_to_group:
                    raise ValueError(f"SKU「{label}」引用了不存在的规格值 key「{key}」")

            groups_of_sku = {key_to_group[k] for k in sku.spec_value_keys}
            if len(groups_of_sku) != len(self.spec_groups):
                raise ValueError(f"SKU「{label}」在某个规格组下选了多个值")

            combo = frozenset(sku.spec_value_keys)
            if combo in seen_combos:
                raise ValueError(f"SKU「{label}」与其他 SKU 的规格组合重复")
            seen_combos.add(combo)

            # ★ 空编码不参与查重：没填编码的 SKU 之间不算"互相重复"，
            #   它们落库时是 NULL，本来就允许共存
            if sku.sku_code is not None:
                if sku.sku_code in codes:
                    raise ValueError(f"商家编码「{sku.sku_code}」重复")
                codes.add(sku.sku_code)

        return self


class SpuUpdateRequest(CamelModel):
    """只允许改这些"不影响已下单订单"的字段。

    规格组合与 SKU 集合**创建后不可变**：订单里存的是 SKU 快照，
    增减 SKU 会让历史订单指向不存在的商品。需要不同规格请新建 SPU。
    """

    category_id: SnowflakeId | None = None
    title: str | None = Field(default=None, min_length=1, max_length=120)
    sub_title: str | None = Field(default=None, max_length=255)
    # None = 不改；"" = 清空（清空后展示层回落占位图）
    main_image: str | None = Field(default=None, max_length=255)
    sort_weight: int | None = None


class SkuUpdateRequest(CamelModel):
    """单个 SKU 的可改字段。价格改动**不影响已下单订单**（订单读快照）。"""

    price: Price | None = None
    # None = 不改；"" = 清空后回落商品主图
    cover_image: str | None = Field(default=None, max_length=255)
    weight_g: Weight | None = None
    status: int | None = Field(default=None, ge=1, le=2)


class SpuAuditRequest(CamelModel):
    approved: bool
    remark: str | None = Field(default=None, max_length=255)


# ============================================================
# 响应
# ============================================================
class SpecValueOut(CamelModel):
    id: SnowflakeId
    value: str
    image: str | None = None


class SpecGroupOut(CamelModel):
    id: SnowflakeId
    name: str
    values: list[SpecValueOut]


class SkuDetailOut(CamelModel):
    id: SnowflakeId
    sku_code: str
    spec_text: str
    price: int
    cover_image: str
    weight_g: int
    status: int
    # 前端规格选择器用它做"可点击性推导"（docs/02 §3）
    spec_value_ids: list[SnowflakeId]


class SpuDetailOut(CamelModel):
    id: SnowflakeId
    shop_id: SnowflakeId
    category_id: SnowflakeId
    title: str
    sub_title: str | None = None
    main_image: str
    price_min: int = Field(description="展示价区间，仅用于列表展示，不能用于下单")
    price_max: int
    total_sold: int
    status: int
    # 最近一次审核意见，**只对店主返回**（买家视角恒为 null）—— 见 service.get_spu_detail
    audit_remark: str | None = None
    spec_groups: list[SpecGroupOut]
    skus: list[SkuDetailOut]


class SpuCardOut(CamelModel):
    """列表/搜索结果的卡片，只含展示需要的字段。"""

    id: SnowflakeId
    shop_id: SnowflakeId
    category_id: SnowflakeId
    title: str
    main_image: str
    # ★ 640 档，**卡片渲染用它**。卡片宽 200~260 CSS px，直接吃 1280 的原图等于
    #   多送 5~6 倍像素；真实照片一张 100~300KB，一屏 12 张就是一两个 MB。
    #   故意保留 main_image 不替换：`_m` 文件缺失时（老上传、手工放进目录的图）
    #   前端要能回退到它，而不是直接裂图。
    main_image_mid: str
    price_min: int
    price_max: int
    total_sold: int
    # ★ 可空：**没有评价时是 null**，前端据此显示"暂无评价"。
    #   给 0.0 会被渲染成"0 分商品"，给 5.0 会被渲染成"满分好评" —— 都在误导
    avg_score: float | None = None
    review_count: int = 0
    status: int = 2


class SkuBriefOut(CamelModel):
    """购物车 / 结算页批量查询 SKU 用。"""

    id: SnowflakeId
    spu_id: SnowflakeId
    shop_id: SnowflakeId
    title: str
    sku_code: str
    spec_text: str
    price: int
    cover_image: str
    weight_g: int
    status: int
    # SPU 的状态。购物车要区分"已下架"与"有效"，只看 SKU 状态不够
    spu_status: int
    # 末级类目。促销的"指定类目"范围匹配要用
    category_id: SnowflakeId


class SkuBatchRequest(CamelModel):
    sku_ids: list[SnowflakeId] = Field(min_length=1, max_length=200)


class SpuListOut(CamelModel):
    items: list[SpuCardOut]
    has_more: bool
    next_cursor: str | None = None
    # ★ **可空**：只有商城搜索接口会数总数，而且只在第一页给。
    #   商家/运营列表页没有这个文案，不该替它们白跑一次 COUNT；
    #   翻页时也早就在首页拿过了。前端拿不到就退回"已显示 N 件"。
    total: int | None = None
