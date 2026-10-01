"""异步任务 worker 进程（ARQ）。

承担三类工作（docs/14-redis-keys.md §6）：
1. 延迟任务：订单超时关单、售后各环节超时
2. 定时任务（cron）：对账、库存对账、流水分区维护、过期券清理
3. 常驻消费：outbox 投递、Redis Streams 消费组

与 api 进程是同一个镜像、不同的启动命令，见 deploy/docker-compose.yml。
"""
