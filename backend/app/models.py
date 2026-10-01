"""ORM 模型注册表。

Alembic 的 autogenerate 只能看到**被导入过**的模型，所以每个新模块的
``models.py`` 都必须在这里 import 一次 —— 这是唯一的集中登记处。
"""

from __future__ import annotations

from app.core.base import Base

# 模块实现后在这里登记，例如：
# from app.modules.core.models import LocalMessage  # noqa: F401
# from app.modules.account.models import User  # noqa: F401
# from app.modules.trade.models import OrderMain, OrderSub, OrderItem  # noqa: F401

__all__ = ["Base"]
