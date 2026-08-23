# 存储层（Store）

负责对数据库（Python SQLite）的操作：Entity 定义与增删改查。

持久化实体：projects / sessions / messages / runs / tasks / attachments / approvals / 非敏感 settings / legacy import 记录。

启用 foreign keys、WAL 和版本化迁移；状态变更在事务内提交后才发事件（见 ADR-006、桌面应用架构）。
