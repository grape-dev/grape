# Skill 通用表重建方案

> 目标：统一收编官方技能、个人技能、仓库技能到一张通用 skill 表，Agent 类型独立成表。
> 关联文档：`database-tables-overview.md`
> 状态：**已实施**

---

## 一、背景

原 skill 相关数据分散在 3 张表，结构混乱：

| 旧表 | 存储 | 问题 |
|------|------|------|
| `agent_type_config` | Agent 类型 + 官方技能混存 | 主键冲突（vod/issue 双重身份），脏数据（issue-skill） |
| `user_skills` | 个人技能 | 仅 user 维度，无 repo/official |
| `repo_skill_mapping` | 仓库技能 | 死表，无实际使用 |

## 二、目标设计

### 表 1：`agent_type` — Agent 类型定义表

```sql
CREATE TABLE agent_type (
    agent_type TEXT PRIMARY KEY,        -- vod/issue/coding/qa/sre
    display_name TEXT NOT NULL,
    description TEXT DEFAULT '',
    default_backend TEXT DEFAULT 'opencode',
    icon TEXT DEFAULT '',
    order_num INTEGER DEFAULT 0,
    subscribe_events TEXT DEFAULT '[]',  -- 事件路由
    created_at TEXT DEFAULT (datetime('now'))
)
```

### 表 2：`skill` — 通用技能表

```sql
CREATE TABLE skill (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL,
    display_name TEXT DEFAULT '',
    description TEXT DEFAULT '',
    agent_type TEXT DEFAULT '',          -- 分类：vod/issue/coding/qa/sre/chat
    skill_path TEXT DEFAULT '',          -- SKILL.md 相对路径
    scope TEXT DEFAULT 'official',       -- official/repo/user
    owner_id TEXT DEFAULT '',            -- repo→repo_key；user→user_id
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE (skill_name, scope, owner_id)
)
```

## 三、可见性规则

| scope | 可见范围 | owner_id | SKILL.md 路径 |
|-------|---------|----------|--------------|
| `official` | 所有仓库/用户 | '' | `skills/{skill_path}/` |
| `repo` | 仅指定仓库 | repo_key | `skills/repo/{repo_key}/{skill_path}/` |
| `user` | 仅指定用户 | user_id | `skills/custom/{user_id}/{skill_path}/` |

## 四、代码改动

| 文件 | 改动 |
|------|------|
| `src/api/db_setup.py` | 新建 agent_type + skill 表；seed 写入新表；迁移逻辑；DROP 旧表 |
| `src/api/routing.py` | 事件路由改查 `agent_type.subscribe_events` |
| `src/api/routes/agents.py` | Agent 元数据改查 `agent_type` 表 |
| `src/api/routes/skills.py` | 技能列表/详情/上传/删除改查 `skill` 表（scope 过滤） |
| `src/api/routes/chat.py` | chat 技能改查 `skill` 表 |
| `src/user/db.py` | create_skill/delete_skill/list_skills 改查 skill 表；删除死代码 repo_skill_mapping 函数 |
| `tests/test_api_routes.py` | 测试数据改写入 agent_type + skill 表 |

## 五、数据迁移

```sql
-- agent_type 表：agent_type_config 中 order_num>0 的行
-- skill 表 official：agent_type_config 中 parent_agent_type!='' 的行
-- skill 表 user：user_skills 的行（scope=user, owner_id=user_id）
-- DROP agent_type_config / user_skills / repo_skill_mapping
```

## 六、验证

- [ ] 迁移后 agent_type 有 5 行（vod/issue/coding/qa/sre）
- [ ] 迁移后 skill 有官方技能（scope=official）
- [ ] 事件路由正常（get_domain_agent 查 agent_type）
- [ ] `/api/agents/types` 返回 5 个 Agent 类型
- [ ] `/api/skills` 返回官方 + 个人技能
- [ ] 幂等性：重复启动无错误
