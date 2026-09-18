# 去掉归档逻辑方案

## 背景

归档机制（Issue #67 设计）当前状态：
- 手动归档接口已删除（前端无调用方）
- Issue 关闭自动归档是唯一触发路径
- 归档后 agent 读不到知识（`_query_knowledge_obs` 加了 archived=0）
- 90 天清理依赖 archived=1 标记
- 当前 DB 0 条归档记录，32 条总数

问题：
1. 归档影响 agent 读取——Issue reopen 后读不到之前沉淀的知识
2. archived=0 维护成本高——20+ 处查询要记得加，漏加就出 bug（本 Issue 已发现 6 处遗漏）
3. 归档的理由"避免干扰新 Issue"不成立——issue_num 过滤已隔离不同 Issue

## 方案：去掉归档逻辑

### 原则

- 不归档、不清理
- `archived=0` 查询条件全部保留不动（所有记录都是 archived=0，查询自然命中）
- `archived` 字段保留在表结构中（不删字段，避免迁移）
- 归档函数和清理函数删除
- 现有 archived=1 的记录恢复为 archived=0（防止孤儿数据）
- `show_archived` 参数删除（无实际效果，留着让人困惑）
- 返回值里的 `archived` 字段保留（前端 knowledge.js 用它做样式，值永远 0 不影响功能）

### 改动清单

#### 1. 删除的函数

| 函数 | 位置 | 说明 |
|------|------|------|
| `_archive_issue_knowledge()` | knowledge.py | Issue 关闭归档，不再调用 |
| `cleanup_archived_knowledge()` | knowledge.py | 90 天清理，不再需要 |

#### 2. 删除的调用

| 调用位置 | 说明 |
|---------|------|
| event_pipeline.py | Issue 关闭时调 `_archive_issue_knowledge`，删除 |
| main.py | 定时调 `cleanup_archived_knowledge`，删除 |

#### 3. 迁移函数加步骤 0：恢复归档数据

在 `_migrate_knowledge` 步骤 1（baseline 去重）之前加：

```sql
UPDATE agent_shared_knowledge SET archived=0 WHERE archived=1
```

把现有 archived=1 的记录恢复为活跃状态，防止变成孤儿数据。

#### 4. 迁移函数步骤 1 简化

两阶段去重（先删 archived=1 有活跃同名的，再 MAX(id)）改回简单 MAX(id) 去重——步骤 0 恢复后所有记录都是 archived=0，不再需要区分。

#### 5. 删除 show_archived 参数

`list_knowledge` 的 `show_archived` 参数、`if not show_archived` WHERE 条件、`ORDER BY archived ASC` 排序全部删除。归档逻辑去掉后所有记录都是 archived=0，参数无实际效果。

#### 6. 不改的

- `archived=0` 查询条件全部保留（13 处，不动——所有记录都是 archived=0，查询自然命中）
- `archived` 字段保留在表 DDL 中（向后兼容）
- `list_knowledge` 和 `knowledge_detail` 返回值仍包含 `archived` 字段（前端 knowledge.js 用 `k.archived` 做样式和按钮显隐，值永远 0，`!k.archived` 为 true 按钮正常显示）
- `stats` 查询里的 `archived=0` 已删除（和 list_knowledge 一致，所有记录 archived=0，条件无实际效果）
- `SELECT` 语句仍查 `archived` 列（不改 SQL 结构）

## 影响

- Issue 关闭后知识保留在 DB，archived 始终为 0
- agent 始终能读到所有知识（issue_num 过滤保证只读当前 Issue 的）
- 不再有 90 天物理清理
- 现有 archived=1 的记录恢复为活跃
- 代码简化：去掉 2 个函数 + 2 处调用 + show_archived 参数 + 迁移函数两阶段去重简化

## 确认点

1. 去掉归档逻辑（不归档、不清理），archived=0 查询条件不动——确认
2. 现有 archived=1 的记录在迁移步骤 0 中恢复为 archived=0——确认
3. archived 字段保留在表里不删——确认
4. Issue 关闭不再归档知识——确认
5. show_archived 参数删除——确认
6. 返回值里的 archived 字段保留（前端用，值永远 0）——确认
