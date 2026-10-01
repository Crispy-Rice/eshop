"""业务模块包。

一个领域一个子包，每个子包内部固定这几个文件：

    models.py      SQLAlchemy 模型（要在 app/models.py 里登记）
    schemas.py     Pydantic 请求/响应模型
    repository.py  数据访问（只碰自己的 schema）
    service.py     领域逻辑，**对其他模块暴露的唯一入口**
    router.py      HTTP 路由

模块之间只允许调用对方的 service 或订阅领域事件，
禁止跨模块直接读写对方的表（docs/01-overview.md §2）。
"""
