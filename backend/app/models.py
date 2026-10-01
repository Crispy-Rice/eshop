"""ORM 模型注册表。

Alembic 的 autogenerate 只能看到**被导入过**的模型，所以每个新模块的
``models.py`` 都必须在这里 import 一次 —— 这是唯一的集中登记处。
"""

from __future__ import annotations

from app.core.base import Base
from app.modules.account.models import (  # noqa: F401
    RefreshToken,
    Shop,
    ShopMember,
    User,
    UserAddress,
)
from app.modules.core.models import LocalMessage  # noqa: F401

# 后续模块在这里登记，例如：
# from app.modules.product.models import Spu, Sku

__all__ = ["Base"]
