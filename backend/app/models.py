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
from app.modules.aftersale.models import RefundItem, RefundOrder  # noqa: F401
from app.modules.cart.models import CartItem  # noqa: F401
from app.modules.core.models import LocalMessage, OpsAlert  # noqa: F401
from app.modules.freight.models import (  # noqa: F401
    FreightExcludeRegion,
    FreightRegionRule,
    FreightTemplate,
    SkuFreightBind,
)
from app.modules.inventory.models import (  # noqa: F401
    SkuStock,
    StockBizKey,
    StockFlow,
    Warehouse,
)
from app.modules.notify.models import SiteMessage  # noqa: F401
from app.modules.payment.models import MockChannelTrade, Payment, PaymentRefund  # noqa: F401
from app.modules.product.models import (  # noqa: F401
    Category,
    Sku,
    SkuSpec,
    SpecGroup,
    SpecValue,
    Spu,
)
from app.modules.promotion.models import (  # noqa: F401
    CouponCode,
    CouponFlow,
    CouponReceiveLog,
    CouponTemplate,
    CouponUserQuota,
    PromoActivity,
    PromoStackRule,
)
from app.modules.review.models import Review, ReviewReply  # noqa: F401
from app.modules.support.models import (  # noqa: F401
    Ticket,
    TicketMessage,
    TicketStateFlow,
)
from app.modules.trade.models import (  # noqa: F401
    DeliveryItem,
    DeliveryOrder,
    OrderDiscountSnapshot,
    OrderItem,
    OrderMain,
    OrderStateFlow,
    OrderSub,
)

# 后续模块在这里登记，例如：
# from app.modules.trade.models import OrderMain, OrderSub

__all__ = ["Base"]
