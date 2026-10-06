"""freight 模块的领域逻辑。

分两块：

- **模板配置**（商家）：建/改模板、区域规则、不发货区域、SKU 绑定，
  以及 docs/06 §12 点名的那些配置校验
- **计算入口**：把"商品行 + 收货区域"解析成引擎需要的 ``FreightItem``，
  再交给纯函数 ``calculator.calc_freight``

解析这一步是 IO，计算那一步不是 —— 分开才能让引擎保持可在单测里复现。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.core.snowflake import next_id
from app.modules.freight import repository as repo
from app.modules.freight.calculator import (
    FreightItem,
    FreightResult,
    FreightRule,
    RegionRuleEntry,
    calc_freight,
    pick_region_rule,
)
from app.modules.freight.models import (
    CHARGE_BY_WEIGHT,
    DEFAULT_TPL_ADD_PRICE,
    DEFAULT_TPL_ADD_UNIT_G,
    DEFAULT_TPL_FIRST_PRICE,
    DEFAULT_TPL_FIRST_UNIT_G,
    DEFAULT_TPL_FREE_THRESHOLD,
    DEFAULT_TPL_NAME,
    REGION_ALL,
    FreightExcludeRegion,
    FreightRegionRule,
    FreightTemplate,
    SkuFreightBind,
)
from app.modules.freight.schemas import (
    ExcludeRegionIn,
    FreightTemplateCreateRequest,
    FreightTemplateUpdateRequest,
    RegionRuleIn,
)

# ★ 依赖方向只允许 freight → product（docs/01 §2）。绑定时要校验 SKU 归属，
#   只能走 product 的 service；product 不反向 import freight，所以不会成环。
from app.modules.product import service as product_service

# 商品的兜底重量（历史脏数据 weight_g = 0 时用），与引擎里的常量一致
FALLBACK_WEIGHT_G = 500


@dataclass(slots=True)
class FreightLine:
    """算价时传进来的一行商品。"""

    sku_id: int
    num: int
    weight_g: int
    amount: int
    shop_id: int
    title: str = ""


# ============================================================
# 配置校验（docs/06 §12）
# ============================================================
def validate_template(req: FreightTemplateCreateRequest | FreightTemplateUpdateRequest) -> None:
    """模板参数的校验。

    ``first_unit`` 的规则有个例外：**按件数计费时它表示"首件数"，可以是 0**
    （等于没有免费额度，第一件就开始收首重价）；按重量时必须是正数，
    否则"首重 0 克"会让计费公式失去意义。
    """
    if req.first_price < 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "首重价不能为负")
    if req.add_price < 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "续重价不能为负")
    if req.add_unit <= 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "续重单位必须大于 0")
    if req.charge_type == CHARGE_BY_WEIGHT and req.first_unit <= 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "按重量计费时首重必须大于 0")
    if req.free_threshold < 0 or req.free_num < 0:
        raise BizError(ErrorCode.VALIDATION_ERROR, "包邮门槛不能为负")


def validate_region_rules(rules: list[RegionRuleIn]) -> None:
    """区域规则的整体校验。

    ★ **必须有"全国默认"规则**（``region_code = "0"``）。否则除了显式配置的
    省市之外，其他地区一条规则都匹配不到，用户结算时会被"该地区不配送"拦住 ——
    而商家的本意显然不是这样。这是 docs/06 §12 点名的第 3 条。
    """
    if not rules:
        return
    if not any(r.region_code == REGION_ALL for r in rules):
        raise BizError(
            ErrorCode.VALIDATION_ERROR,
            '配置了区域规则就必须同时保留一条"全国默认"，否则其他地区无法下单',
        )
    seen: set[str] = set()
    for r in rules:
        if r.region_code in seen:
            raise BizError(ErrorCode.VALIDATION_ERROR, f"区域 {r.region_code} 重复配置")
        seen.add(r.region_code)
        if r.add_unit <= 0:
            raise BizError(ErrorCode.VALIDATION_ERROR, "续重单位必须大于 0")
        if r.first_price < 0 or r.add_price < 0:
            raise BizError(ErrorCode.VALIDATION_ERROR, "运费不能为负")


def validate_exclude_regions(
    exclude_codes: Sequence[str], delivered_codes: set[str]
) -> None:
    """不发货区域不能与已配置的配送区域冲突（docs/06 §12 第 4 条）。

    只挡住"精确同码"的情况 —— 省级排除 + 市级配送这种更细的冲突判断起来很绕，
    而且实际业务里商家不会这么配。**宁可少判，不要误判**。
    """
    for code in exclude_codes:
        if code in delivered_codes:
            raise BizError(
                ErrorCode.VALIDATION_ERROR,
                f"区域 {code} 既配了配送规则又配了不发货，请二选一",
            )


# ============================================================
# 模板 CRUD
# ============================================================
async def create_template(
    session: AsyncSession, shop_id: int, req: FreightTemplateCreateRequest
) -> FreightTemplate:
    validate_template(req)
    # 店铺的第一条模板自动成为默认。否则"未绑定的 SKU 回落到默认模板"这件事
    # 要等商家主动去点一下才生效，而在此之前新发布的商品照样是坏的 ——
    # 一个只有一条模板的店，那条模板显然就是它的默认。
    is_default = await repo.get_default_template(session, shop_id) is None
    tpl = FreightTemplate(
        id=next_id(),
        shop_id=shop_id,
        name=req.name,
        charge_type=req.charge_type,
        first_unit=req.first_unit,
        first_price=req.first_price,
        add_unit=req.add_unit,
        add_price=req.add_price,
        free_shipping=req.free_shipping,
        free_threshold=req.free_threshold,
        free_num=req.free_num,
        merge_type=req.merge_type,
        status=1,
        is_default=is_default,
    )
    return await repo.insert_template(session, tpl)


async def ensure_default_template(session: AsyncSession, shop_id: int) -> FreightTemplate:
    """给店铺建一条"开箱即用"的默认模板 —— **开店时**由 account 调用。

    ★ 没有它，新店的商品**一件都上不了架**（上架要求每个规格都算得出运费），
      而商家要绕到"提交审核 → 平台点通过被判 400"才会发现。
    ★ 参数是**起点不是真理**：首重 / 续重 / 包邮门槛都该由商家按真实运费改，
      所以名字就叫「默认快递模板」，列表里也标着「默认」，一眼看得出是系统给的。
    ★ 幂等：店里已经有模板就不动 —— 重试、重复调用都不会多建。
    """
    existing = await repo.list_templates(session, shop_id)
    if existing:
        return existing[0]

    tpl = await create_template(
        session,
        shop_id,
        FreightTemplateCreateRequest(
            name=DEFAULT_TPL_NAME,
            first_unit=DEFAULT_TPL_FIRST_UNIT_G,
            first_price=DEFAULT_TPL_FIRST_PRICE,
            add_unit=DEFAULT_TPL_ADD_UNIT_G,
            add_price=DEFAULT_TPL_ADD_PRICE,
            free_threshold=DEFAULT_TPL_FREE_THRESHOLD,
        ),
    )
    # 顺手配一条「全国默认」区域规则：不配也能算（全国按模板本身计费），
    # 但商家以后想加"上海另计"时，校验会要求规则里必须有一条全国默认 ——
    # 给一个形状正确的起点，让他少踩一次。
    await replace_region_rules(
        session,
        shop_id,
        int(tpl.id),
        [
            RegionRuleIn(
                region_code=REGION_ALL,
                first_unit=DEFAULT_TPL_FIRST_UNIT_G,
                first_price=DEFAULT_TPL_FIRST_PRICE,
                add_unit=DEFAULT_TPL_ADD_UNIT_G,
                add_price=DEFAULT_TPL_ADD_PRICE,
            )
        ],
    )
    return tpl


async def set_default_template(
    session: AsyncSession, shop_id: int, template_id: int
) -> FreightTemplate:
    """把某条模板设为店铺默认。

    ★ 停用的模板不能当默认：默认是"没绑定时的兜底"，一条停用的兜底等于没有兜底，
      却会让商家以为已经配好了（比没设更坏，因为它看起来是配好的）。
    """
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")
    if tpl.status != 1:
        raise BizError(ErrorCode.VALIDATION_ERROR, "停用的模板不能设为默认")

    await repo.set_default_template(session, shop_id=shop_id, template_id=template_id)
    # 上面走的是批量 UPDATE，identity map 里的对象要显式刷一下才拿到新值
    await session.refresh(tpl)
    return tpl


async def update_template(
    session: AsyncSession, shop_id: int, template_id: int, req: FreightTemplateUpdateRequest
) -> tuple[FreightTemplate, int]:
    """改模板。返回 ``(模板, 受影响的 SKU 数)``。

    影响面要给商家看：改了之后**新建订单立刻生效，已下单的不受影响**
    ——因为订单里存的是运费的计算快照（docs/06 §12 第 6 点）。
    """
    validate_template(req)
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")

    tpl.name = req.name
    tpl.charge_type = req.charge_type
    tpl.first_unit = req.first_unit
    tpl.first_price = req.first_price
    tpl.add_unit = req.add_unit
    tpl.add_price = req.add_price
    tpl.free_shipping = req.free_shipping
    tpl.free_threshold = req.free_threshold
    tpl.free_num = req.free_num
    tpl.merge_type = req.merge_type
    tpl.status = req.status

    # 影响面与列表里的"已绑定 N 个 SKU"用同一口径：已软删商品不算进去，
    # 否则这句提示里的数字跟商家刚在列表上看到的对不上。
    deleted = await product_service.list_deleted_sku_ids(session, shop_id)
    affected = await repo.count_binds_of_template(session, template_id, exclude_sku_ids=deleted)
    return tpl, affected


async def replace_region_rules(
    session: AsyncSession, shop_id: int, template_id: int, rules: list[RegionRuleIn]
) -> int:
    """整体替换区域规则。返回规则条数。

    整体替换而不是增删改：区域规则是一组配置，逐条 diff 的复杂度远高于收益。
    """
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")

    validate_region_rules(rules)
    # 与现有的不发货区域做一次交叉校验，避免两边配了同一个区域
    existing_excludes = await repo.list_exclude_regions(session, template_id)
    validate_exclude_regions(
        [r.region_code for r in rules],
        {e.region_code for e in existing_excludes},
    )

    await repo.delete_region_rules(session, template_id)
    if rules:
        await repo.insert_region_rules(
            session,
            [
                FreightRegionRule(
                    id=next_id(),
                    template_id=template_id,
                    region_code=r.region_code,
                    region_level=r.region_level,
                    first_unit=r.first_unit,
                    first_price=r.first_price,
                    add_unit=r.add_unit,
                    add_price=r.add_price,
                    free_shipping=r.free_shipping,
                    enabled=r.enabled,
                    priority=r.priority,
                )
                for r in rules
            ],
        )
    return len(rules)


async def replace_exclude_regions(
    session: AsyncSession, shop_id: int, template_id: int, excludes: list[ExcludeRegionIn]
) -> int:
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")

    rules = await repo.list_region_rules(session, template_id)
    validate_exclude_regions(
        [e.region_code for e in excludes],
        {r.region_code for r in rules if r.region_code != REGION_ALL},
    )

    await repo.delete_exclude_regions(session, template_id)
    if excludes:
        await repo.insert_exclude_regions(
            session,
            [
                FreightExcludeRegion(
                    id=next_id(),
                    template_id=template_id,
                    region_code=e.region_code,
                    reason=e.reason,
                )
                for e in excludes
            ],
        )
    return len(excludes)


async def bind_sku(
    session: AsyncSession,
    shop_id: int,
    *,
    sku_id: int,
    template_id: int,
    warehouse_id: int,
    priority: int = 0,
) -> None:
    """把 SKU 绑到运费模板。同一 (SKU, 模板, 仓库) 重复绑定时更新优先级。"""
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")

    # ★ 必须校验 SKU 属于本店。只查模板归属不够 —— 直接构造请求就能拿别人的
    #   sku_id 把竞争对手的商品绑到自己的模板上（建个 0 元模板、优先级拉满，
    #   对方的商品就对买家包邮了）。
    #   ``batch_get_skus`` 只认没被软删的商品，所以顺带挡住了"给已删商品建绑定"。
    skus = await product_service.batch_get_skus(session, [sku_id], only_on_shelf=False)
    if not skus or skus[0].shop_id != shop_id:
        raise BizError(ErrorCode.NOT_FOUND, "SKU 不存在")

    await repo.upsert_bind(
        session,
        sku_id=sku_id,
        template_id=template_id,
        warehouse_id=warehouse_id,
        priority=priority,
        bind_id=next_id(),
    )


async def list_binds(
    session: AsyncSession, shop_id: int, template_id: int
) -> list[SkuFreightBind]:
    """某个模板绑了哪些 SKU。

    先校验模板属于本店 —— 否则拿别人的 template_id 就能问出"他绑了哪些 SKU"。
    与 ``bind_sku`` 同样的归属检查，不存在与无权一律回 404（不区分两者）。
    """
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")
    return await repo.list_binds_by_template(session, template_id)


async def list_templates(session: AsyncSession, shop_id: int) -> list[FreightTemplate]:
    return await repo.list_templates(session, shop_id)


async def list_region_rules(session: AsyncSession, shop_id: int, template_id: int):
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")
    return await repo.list_region_rules(session, template_id)


async def list_exclude_regions(session: AsyncSession, shop_id: int, template_id: int):
    tpl = await repo.get_shop_template(session, shop_id, template_id)
    if tpl is None:
        raise BizError(ErrorCode.NOT_FOUND, "运费模板不存在")
    return await repo.list_exclude_regions(session, template_id)


# ============================================================
# 给其它模块的只读查询
# ============================================================
async def skus_without_freight(
    session: AsyncSession, *, shop_id: int, sku_ids: Sequence[int]
) -> list[int]:
    """这些 SKU 里**既没绑定模板、也没有店铺默认模板可回落**的那些。

    ★ 给 product 的「上架 / 审核通过」做前置校验用。这个漏检原先只在**买家结算**
      才暴露（``estimate`` 里报错），那时商品已经挂在架上了 —— 上架是最后一道
      能拦住"卖不出去的商品"的关口。

    判定必须与 ``estimate`` 的报错条件**逐个对齐**，否则会出现"上架放行、结算报错"：
    绑到启用中的模板 → 通过；没绑定但店铺有启用中的默认模板 → 通过；
    绑到**停用**的模板 → **不通过**（estimate 对它报"模板已停用"，不会回落）。
    """
    if not sku_ids:
        return []

    bind_tpl_of = {b.sku_id: b.template_id for b in await repo.list_binds_by_skus(session, sku_ids)}
    tpls = {
        t.id: t for t in await repo.list_templates_by_ids(session, list(set(bind_tpl_of.values())))
    }
    default = await repo.get_default_template(session, shop_id)
    usable_default = default is not None and default.status == 1

    missing: list[int] = []
    for sku_id in sku_ids:
        bound_tpl = tpls.get(bind_tpl_of[sku_id]) if sku_id in bind_tpl_of else None
        if bound_tpl is not None and bound_tpl.status == 1:
            continue
        if sku_id not in bind_tpl_of and usable_default:
            continue
        missing.append(sku_id)
    return missing


# ============================================================
# 计算入口
# ============================================================
def _to_rule(tpl: FreightTemplate, region) -> FreightRule:
    """模板 + 区域规则 → 最终计费规则。区域规则优先级高于模板默认值。"""
    if region is None:
        return FreightRule(
            charge_type=tpl.charge_type,
            first_unit=tpl.first_unit,
            first_price=tpl.first_price,
            add_unit=tpl.add_unit,
            add_price=tpl.add_price,
            free_shipping=tpl.free_shipping,
            free_threshold=tpl.free_threshold,
        )
    return FreightRule(
        charge_type=tpl.charge_type,
        first_unit=region.first_unit,
        first_price=region.first_price,
        add_unit=region.add_unit,
        add_price=region.add_price,
        free_shipping=region.free_shipping or tpl.free_shipping,
        free_threshold=tpl.free_threshold,
    )


async def estimate(
    session: AsyncSession,
    *,
    lines: Sequence[FreightLine],
    region_code: str,
    warehouses: dict[int, int],
) -> FreightResult:
    """算整单运费。

    :param warehouses: ``sku_id -> warehouse_id``，由调用方从库存模块取。
        **没有发货仓的 SKU 直接拒绝** —— 不知道该从哪发，就算不出运费。
    """
    if not lines:
        return FreightResult(total=0, packages=[], notices=[])

    sku_ids = [ln.sku_id for ln in lines]
    binds = await repo.list_binds_by_skus(session, sku_ids)

    # 每个 SKU 取优先级最高的那条绑定
    bind_of: dict[int, SkuFreightBind] = {}
    for b in binds:
        bind_of.setdefault(b.sku_id, b)

    # ★ 没绑定的 SKU 回落到**本店默认模板**（docs/06 §404 写的那半句）。
    #   绑定是逐条 SKU 的，新发布的商品天然在模板之外 —— 没有这条回落，
    #   商家只有等买家在结算页撞上"还没绑定运费模板"时才知道自己漏配了。
    defaults = {
        t.shop_id: t
        for t in await repo.list_default_templates(session, list({ln.shop_id for ln in lines}))
    }

    # 真正会用到的模板 = 命中绑定的 + 回落用到的默认模板。
    # 默认模板的区域规则与不发货区域**一样要生效**，所以它的 id 也要收进来。
    used_ids = {b.template_id for b in bind_of.values()}
    for ln in lines:
        if ln.sku_id not in bind_of:
            fallback = defaults.get(ln.shop_id)
            if fallback is not None:
                used_ids.add(fallback.id)

    templates = {t.id: t for t in await repo.list_templates_by_ids(session, list(used_ids))}
    region_rules = await repo.list_region_rules_by_templates(session, list(used_ids))
    excludes = await repo.list_exclude_by_templates(session, list(used_ids))

    # 按模板分组，避免每条 SKU 都重新筛一遍
    rules_of: dict[int, list[FreightRegionRule]] = {}
    for r in region_rules:
        rules_of.setdefault(r.template_id, []).append(r)
    exclude_of: dict[int, set[str]] = {}
    for e in excludes:
        exclude_of.setdefault(e.template_id, set()).add(e.region_code)

    items: list[FreightItem] = []
    for ln in lines:
        bind = bind_of.get(ln.sku_id)
        if bind is not None:
            tpl = templates.get(bind.template_id)
            if tpl is None or tpl.status != 1:
                raise BizError(ErrorCode.SKU_NOT_SUPPORTED, f"「{ln.title}」的运费模板已停用")
        else:
            # 没绑定 → 用本店默认模板兜底；连默认都没有才是真算不出来
            tpl = defaults.get(ln.shop_id)
            if tpl is None:
                raise BizError(
                    ErrorCode.SKU_NOT_SUPPORTED,
                    f"「{ln.title or ln.sku_id}」还没有运费模板：既没绑定模板，"
                    "店铺也没有设默认模板",
                )
            if tpl.status != 1:
                raise BizError(
                    ErrorCode.SKU_NOT_SUPPORTED,
                    f"「{ln.title}」的店铺默认运费模板已停用",
                )

        # ① 不发货区域：直接拦住，不进计算
        if _region_excluded(exclude_of.get(tpl.id, set()), region_code):
            raise BizError(
                ErrorCode.NOT_DELIVERABLE,
                f"「{ln.title or ln.sku_id}」暂不支持配送至该地区",
            )

        # ② 区域规则覆盖模板默认值
        entries = [
            RegionRuleEntry(
                region_code=r.region_code,
                region_level=r.region_level,
                priority=r.priority,
                rule=_to_rule(tpl, r),
            )
            for r in rules_of.get(tpl.id, [])
            if r.enabled
        ]
        picked = pick_region_rule(entries, region_code) if entries else None
        rule = picked.rule if picked else _to_rule(tpl, None)

        items.append(
            FreightItem(
                sku_id=ln.sku_id,
                num=ln.num,
                weight_g=ln.weight_g if ln.weight_g > 0 else FALLBACK_WEIGHT_G,
                amount=ln.amount,
                warehouse_id=warehouses.get(ln.sku_id, 0),
                template_id=tpl.id,
                title=ln.title,
                rule=rule,
            )
        )

    return calc_freight(
        items,
        template_names={t.id: t.name for t in templates.values()},
        free_num_of={t.id: t.free_num for t in templates.values()},
        merge_type_of={t.id: t.merge_type for t in templates.values()},
    )


def _region_excluded(excluded: set[str], region_code: str) -> bool:
    """收货区域是否在排除名单里（支持前缀：排除整个省就挡住该省所有市）。"""
    if not region_code:
        return False
    return any(
        code == REGION_ALL or region_code == code or region_code.startswith(code)
        for code in excluded
        if code
    )
