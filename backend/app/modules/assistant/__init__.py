"""assistant 模块 —— 后台端的 AI 助手（商家 / 平台运营），见 docs/20-assistant.md。

P0 的三条边界：**只做后台端**、**工具只读**、**答案靠轮询取**。

★ 一处**有意的架构偏离**，先说在这里：项目铁律是"跨模块编排写在 router.py"，
但助手的入口不是 HTTP 而是**工具调用循环**，所以跨模块的组合落在 ``tools.py``
的各个 handler 里（每个 handler 是一个微型编排器），``router.py`` 只管助手自身的
HTTP 编排。除此之外铁律不破：handler 只调对方模块的 ``service.py``，
绝不 import 别人的 models / repository。
"""
