"""account 模块对外暴露的 FastAPI 依赖。其他模块可以 import 这里。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.deps import CurrentUserDep, DbSession
from app.core.errors import BizError, ErrorCode
from app.modules.account.service import get_shop_id


async def get_current_shop_id(user: CurrentUserDep, session: DbSession) -> int:
    """当前用户的店铺 ID。

    ★ 从数据库查，而不是读 JWT 里的 ``shop_id``：用户开店后拿的还是旧 token，
    旧 token 里没有 shop_id。以数据库为准才不会出现"开完店立刻发不了商品"。
    """
    shop_id = await get_shop_id(session, user.id)
    if shop_id is None:
        raise BizError(ErrorCode.FORBIDDEN, "请先开通店铺")
    return shop_id


CurrentShopIdDep = Annotated[int, Depends(get_current_shop_id)]
