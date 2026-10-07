"""路由聚合。

每个模块在自己的 ``router.py`` 里定义 ``router = APIRouter(prefix="/api/...")``，
在这里统一挂载。按路径前缀分三组：

- 买家端   /api/...            登录即可，不校验角色
- 商家端   /api/merchant/...   ``CurrentShopIdDep``（account/deps.py）按**店铺归属**校验
- 运营端   /api/admin/...      ``require_role("admin", "finance")``

★ 商家端**刻意不看 role**。``role`` 是签发 token 那一刻的快照：用户开完店，
数据库里已经是 ``merchant``，手里那张 token 仍然写着 ``buyer``，要等下次刷新
才更新（account/service.py 的 ``create_shop`` 只改库，不重签）。用店铺归属判权
既比角色更精确（角色是粗粒度的，店铺是资源级的），也不会被过期 token 坑。

模块实现后把对应 import 打开即可（docs/01-overview.md §2.1）。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from app.modules.account.router import router as account_router
from app.modules.aftersale.router import router as aftersale_router
from app.modules.assistant.router import router as assistant_router
from app.modules.cart.router import router as cart_router
from app.modules.files.router import router as files_router
from app.modules.freight.router import router as freight_router
from app.modules.inventory.router import router as inventory_router
from app.modules.notify.router import router as notify_router
from app.modules.payment.router import router as payment_router
from app.modules.product.router import router as product_router
from app.modules.promotion.router import router as promotion_router
from app.modules.review.router import router as review_router
from app.modules.support.router import router as support_router
from app.modules.trade.router import router as trade_router

MODULE_ROUTERS: list[APIRouter] = [
    account_router,
    product_router,
    inventory_router,
    cart_router,
    promotion_router,
    freight_router,
    trade_router,
    payment_router,
    aftersale_router,
    files_router,
    review_router,
    support_router,
    notify_router,
    assistant_router,
]


def register_routers(app: FastAPI) -> None:
    for router in MODULE_ROUTERS:
        app.include_router(router)
