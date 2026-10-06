"""发货仓路由：按收货区划决定"这个店铺发往某地用哪个仓"。

## 地基：规则优先，缺货才按候选链兜底

路由的**主体**仍然是纯规则：命中区域规则的仓优先，都没命中走默认仓。在此之上，
**当规则仓盖不住这一单的货时可以往后退** —— 候选顺序是
`规则仓 → 默认仓 → 其余启用仓（id 升序）`，由 `service.route_warehouse` 挑第一个
**能覆盖整单**的仓（那个函数才是唯一做库存判断的地方，本模块保持纯函数）。

### v1 那句"绝不看库存"是怎么被推翻的

v1 这里写的是"路由**不得**因为某仓有货而改选仓"，理由是"怕算价选 A、下单选 B 导致
运费变化，**误触发 priceToken 的价格一致性校验**"。这条理由**双重不成立**：

1. **那套校验根本没实现。** 商城的 `calcPrice` 请求/响应里没有 token，下单请求
   （`OrderCreateRequest`）里也**没有金额字段**，服务端无从比对；
   `price_token_secret` / `PriceChangedError` / `PRICE_TOKEN_EXPIRED` 全是声明未用。
   全项目唯一抛 `PRICE_CHANGED` 的地方是 `trade` 的**母子单守恒断言**，
   与"用户看到的价格变了"无关。
2. **即使它实现了，兜底也不改价**：模板按 **`sku_id`** 取（与仓无关，见
   `freight.repository.list_binds_by_skus`），包裹按仓分组，而"一个地址落一个仓"
   意味着子单的所有商品本来就在同一个仓 —— 换仓之后**还是**在同一个仓，
   包裹数、每包重量与模板逐项不变。

★★ **第 2 条有个前提，改这一带之前先读**：它成立是因为 `list_binds_by_skus` 今天
**只按 `sku_id` 过滤、不看 `warehouse_id`**。而 `SkuFreightBind` 的模型注释与
`docs/06 §1` 都写着要"**按实际发货仓筛选**" —— 那列是**半成品**。哪天真把按仓绑定接上，
"兜底换仓"就会改价：那时要么只在运费相同时才换，要么给订单加价格确认。

### 不变的推论

1. 同一个子单的所有商品**必然发往同一个仓**（同店 + 同地址 + 整单择仓），所以仓记在
   `order_sub` 上就够，也**不会**出现"一个子单要拆成多张发货单"。★ 代价：
   **整单的货分散在不同仓、没有哪个仓能一次盖住 → 整单失败**，不做逐件挑仓
   （那会把交易粒度降到 (子单, SKU, 仓)）。文案会提示"分开下单"——那真的管用，
   因为择仓是**按这一单**判的。
2. 商家事后改区域规则，不会影响已经在途的订单 —— 那些订单读的是记录值，不再路由。

## 为什么不需要优先级

规则表的唯一键是 ``(shop_id, region_code)``：**一个区划码只能由一个仓发货**，
所以不存在"同码择优"。具体性完全由**码长**决定 —— "4403 深圳 → 深圳仓" 比
"44 广东 → 广州仓" 更具体，收货地址 ``440305`` 两条都命中，取码更长的那个。
商家的心智也因此简单：一个地方只由一个仓发货。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.modules.inventory.models import (
    WAREHOUSE_ENABLED,
    Warehouse,
    WarehouseRegionRule,
)

# 区划码 "0" = 该仓兜底覆盖全国。与 freight 的 REGION_ALL 同一个哨兵值（docs/06 §5）
REGION_ALL = "0"


def pick_rule(
    rules: Sequence[WarehouseRegionRule], region_code: str
) -> WarehouseRegionRule | None:
    """挑出命中的规则：前缀匹配 + **码越长越具体**。

    ★ ``"0"``（全国兜底）走 ``==`` 而不是前缀匹配 —— 否则任何以 ``"0"`` 开头的
      区划码都会被它吃掉（真实区划码都以非 0 开头，这是防呆）。

    ★ 同长度不可能同时命中：同一长度的前缀是唯一的。所以比较里带 ``id`` 只是
      **兜底保证确定性**（结果不依赖行的返回顺序），不是业务上的"优先级"。
    """
    if not region_code:
        return None
    matched = [
        r
        for r in rules
        if r.region_code == REGION_ALL or region_code.startswith(r.region_code)
    ]
    if not matched:
        return None
    return max(matched, key=lambda r: (len(r.region_code), r.id))


def pick_warehouse(
    *,
    rules: Sequence[WarehouseRegionRule],
    enabled: Mapping[int, Warehouse],
    default_wh: Warehouse | None,
    region_code: str,
) -> Warehouse | None:
    """命中的规则用它指的仓；否则落到默认仓。都不行就是 ``None``（该店没有可用的仓）。

    ``enabled`` 只装**启用中**的仓，所以"规则指向的仓已停用"会自然落空、继续走默认仓 ——
    停用的语义是"这个仓不再接新单"，但不该让整个地区无法下单。
    （历史订单落在停用仓上的照常发货 —— 那条路径读的是订单上记录的仓，根本不走这里。）

    ★ **它只按规则，不看库存**（库存是 IO）。真正的下单/算价走
      ``service.route_warehouse``，它会在 :func:`warehouse_candidates` 给出的顺序上
      再按库存往后退。两者的关系：只要该店有规则或默认仓，本函数与候选链的**第一个**
      就是同一个仓；差别只在"规则与默认都没有、却有别的启用仓"这种状态 ——
      那是一期不变量（每店必有且仅有一个默认仓）被破坏的情况，这里保持保守地返回
      ``None``，而候选链会用那个仓。
    """
    rule = pick_rule(rules, region_code)
    if rule is not None:
        warehouse = enabled.get(int(rule.warehouse_id))
        if warehouse is not None:
            return warehouse
    return default_wh


def warehouse_candidates(
    *,
    rules: Sequence[WarehouseRegionRule],
    enabled: Mapping[int, Warehouse],
    default_wh: Warehouse | None,
    region_code: str,
) -> list[Warehouse]:
    """候选仓的**顺序**：``规则仓 → 默认仓 → 其余启用仓（id 升序）``。

    ★ 纯函数、**不看库存** —— 库存是 IO，由 ``service.route_warehouse`` 按这个顺序扫。
      这样"什么顺序"只有一处定义，而单测不必连库。
    ★ 去重：规则仓常常**就是**默认仓，别让它出现两次（否则白查一次库存）。
    ★ 顺序里带"其余的启用仓"是**兜底**：规则仓没货时还能从别的仓发（见模块文档）。
      停用的仓不在 ``enabled`` 里，天然被跳过。
    """
    out: list[Warehouse] = []
    seen: set[int] = set()

    def take(warehouse: Warehouse | None) -> None:
        if warehouse is not None and int(warehouse.id) not in seen:
            seen.add(int(warehouse.id))
            out.append(warehouse)

    rule = pick_rule(rules, region_code)
    if rule is not None:
        take(enabled.get(int(rule.warehouse_id)))
    take(default_wh)
    for warehouse_id in sorted(enabled):
        take(enabled[warehouse_id])
    return out


def is_enabled(warehouse: Warehouse) -> bool:
    return warehouse.status == WAREHOUSE_ENABLED


def region_level_of(code: str) -> int:
    """由区划码长度推导层级：2 位 = 省 / 4 位 = 市 / 6 位 = 区。

    ``"0"``（全国兜底）按省级记。这个值**只作展示**，匹配时不参与 ——
    参与的是码长本身（见 :func:`pick_rule`）。与 freight 的取舍一致。
    """
    return {2: 1, 4: 2, 6: 3}.get(len(code), 1)
