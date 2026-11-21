# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、运行、复核和报表分发动作。
- `stocktake_batches` 扫码实盘批次：月末工具快照、当前扫码账、对账结果、版本与重算租约。
- `stocktake_scans` 只追加的扫码流水，`(batch_id, scan_id)` 唯一约束拒绝并发重复补扫。
- `stocktake_dispute_groups` 最近一次重算后的待核组（整体替换，`(batch_id, group_seq)` 唯一）。
- `stocktake_dispute_resolutions` 主管对争议组的人工核示留痕。
