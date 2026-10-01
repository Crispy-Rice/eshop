"""路由聚合。

每个模块在自己的 ``router.py`` 里定义 ``router = APIRouter(prefix="/api/...")``，
在这里统一挂载。按路径前缀分三组：

- 买家端   /api/...
- 商家端   /api/merchant/...   require_role("merchant")
- 运营端   /api/admin/...      require_role("admin", "finance")

模块实现后把对应 import 打开即可（docs/01-overview.md §2.1）。
"""

from __future__ import annotations

from fastapi import FastAPI

# from app.modules.product.router import router as product_router
# from app.modules.trade.router import router as trade_router
# ... 其余模块同理

MODULE_ROUTERS: list = [
    # product_router,
    # trade_router,
]


def register_routers(app: FastAPI) -> None:
    for router in MODULE_ROUTERS:
        app.include_router(router)
