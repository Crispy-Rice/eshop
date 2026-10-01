"""评价模块：商品评价、追评、审核与商家回复。

对应 docs/12-review.md。**核心是"购后限制"与"唯一性"**（docs/01 §2 给这个模块
标的两条关键难点）：

- **购后限制**：必须有真实的已完成订单项才能评价。判定不看子单状态，见下面第 2 条。
- **唯一性**：`uk_review_order_item (order_item_id) WHERE NOT is_follow_up`
  保证每个订单项只能有一条首评；`uk_review_follow_up (parent_id) WHERE is_follow_up`
  保证一条首评只能追评一次。**唯一索引才是保证，服务层的预检查只是友好提示。**

三条容易做错、这里刻意钉住的规则：

1. **`ON CONFLICT DO NOTHING` 而不是捕获 `IntegrityError`**：PostgreSQL 里唯一冲突会
   让整个事务进入 aborted 状态，后续语句全报错；`ON CONFLICT DO NOTHING` 冲突时不报错、
   `RETURNING` 拿到 NULL，把它映射成"已经评价过了"即可（docs/12 §2.2）。
2. **★ 部分退款后子单会变成 70「已退款」**（aftersale 对任何退款成功都发
   `REFUND_SUCCESS`）。所以资格判定**不能看 `sub.status == FINISHED`**，
   否则部分退款后剩余商品永远评不了。判定以 `sub.receive_time` + **订单项级**退款数为准。
3. **统计计数与状态迁移必须成对**：发布 +1、屏蔽 −1、解除 +1、驳回不动。
   把两者封在一个 `transit()` 里，杜绝"改了状态忘了改计数"。

不在本期：评价送积分（本库没有积分体系）、搜索排序分、图片机审、申诉流程。
"""
