# Issue #190 设计方案：项目知识库重复 + obs_key/file_path 统一修复

> Branch: fix/issue-190-duplicate-baseline (based on weekly-03-hc)
> Date: 2026-09-18 (updated after multiple review rounds)

## 1. 问题本质

`workspace_repo_dir()` 在某次迭代后路径增加了 provider 段（`gitcode/`、`github/`），但 `obs_key` 生成逻辑全部使用 `split_owner_repo()`（不含 provider）。多个问题叠加产生系统性缺陷。

### 1.1 file_path 三种历史格式

| 格式 | 示例 | 说明 |
|------|------|------|
| OLD | `project-info/member/user/file.md` | 最早期，只有 project-info/ 前缀 |
| PARTIAL | `owner/repo/project-info/xxx.md` | 中期，有 owner/repo 但无 provider |
| NEW | `gitcode/owner/repo/project-info/xxx.md` | 当前代码写入，含 provider 前缀 |

影响：
- baseline 去重依赖 file_path → PARTIAL 和 NEW 格式的同一文件匹配不到 → 产生重复行
- 所有 `os.path.join(WORKSPACE_DIR, file_path)` 路径拼接在 OLD 格式下找不到文件

### 1.2 obs_key 跨 provider 冲突

所有 obs_key 生成点用 `split_owner_repo()`，不含 provider 前缀。同名仓库跨 provider 时 OBS 对象互相覆盖。

### 1.3 obs_key 三种历史格式 + member 路径重复

| 格式 | 示例 | 说明 |
|------|------|------|
| OLD_A | `project-info/gitcode:{owner}/{repo}/{fname}` | 最早期 baseline，不含 knowledge/ 前缀 |
| OLD_B | `knowledge/{owner}/{repo}/...` | 大多数，不含 provider |
| member 重复 | `knowledge/{owner}/{repo}/{owner}/{repo}/member/...` | sync 路径 replace 逻辑导致 owner/repo 重复 |

### 1.4 废弃接口和死代码

以下接口/函数依赖 file_path 做 WHERE 查询，前端已无调用方，属于废弃代码：

| 废弃接口/函数 | 说明 |
|---|---|
| `GET /knowledge/content` `read_knowledge_content` | 前端走 `GET /knowledge/{id}` |
| `GET /knowledge/context` `knowledge_context` | 无任何调用方 |
| `DELETE /knowledge` (query param) `delete_knowledge` | 前端走 `DELETE /knowledge/{id}` |
| `_read_file()` | 只被 `read_knowledge_content` 调用 |
| `_read_ws_file()` | 无任何调用方 |
| `_member_docs()` | 唯一调用方 `knowledge_context` 已删除，变为死代码 |
| `_fmt_time()` | 只被 `_member_docs` 调用 |
| `_owner_repo()` | knowledge.py 和 routes/knowledge.py 各一份，obs_key 改用 knowledge_obs_prefix 后无人调用 |
| `MAX_UPLOAD_BYTES` | upload 接口删除后变为死代码 |
| `_safe_rel()` / `ALLOWED_EXTS` | 只被废弃函数使用 |

## 2. file_path 依赖点全梳理

| 位置 | 用法 | 处理方式 |
|------|------|---------|
| builder.py `_write_to_obs()` | 去重 WHERE file_path=? | 改用 name 查重 |
| knowledge.py `_sync_baseline_files()` | 去重 WHERE file_path=? | 改用 name 查重 |
| knowledge.py `read_entry_content()` | 本地兜底路径拼接 | 加 `workspace_repo_dir()` 兜底 |
| knowledge.py `_do_write_knowledge()` 内容去重 | 查同 scope 已存在记录 | 加 `archived=0` |
| knowledge.py `_do_write_knowledge()` INSERT 查重 | 查同 scope 已存在记录 | 加 `archived=0` |
| knowledge.py `cleanup_archived_knowledge()` | 删本地文件 | 加 `workspace_repo_dir()` 兜底 |
| knowledge.py `knowledge_sync_loop()` | 读本地文件 | 已有 `workspace_repo_dir()` 兜底 |
| knowledge.py `member_files_pi()` | 从 file_path 提取 member/ 相对路径 | 安全：`if "project-info/" in file_path` 对三种格式均成立 |
| routes/knowledge.py `_update_knowledge_content()` | 写文件 | 加 `workspace_repo_dir()` 兜底 |
| routes/knowledge.py `_delete_knowledge_full()` | 删本地文件 | 加 `workspace_repo_dir()` 兜底 |
| routes/knowledge.py 废弃接口 | WHERE file_path=? | 删除 |

## 3. 并发场景分析

### 3.1 现有锁一览

| 路径 | 锁 | 位置 |
|------|-----|------|
| maybe_refresh_project_info | `project_info_build:{md5(repo_key)[:16]}`, TTL 300s | builder.py |
| knowledge_write_worker | `knowledge_write:{md5(...)} `, TTL 60s | knowledge.py |
| knowledge_sync_loop | meta 表 `knowledge_sync_running` 标记 + 10 分钟超时 | knowledge.py |
| init_database (MySQL) | `GET_LOCK('grape_migrate', 300)` 跨实例互斥 | db_setup.py |
| _write_to_obs 内部 | `db_lock` | builder.py |
| _do_write_knowledge 内部 | `db_lock` + 内容去重 | knowledge.py |
| _sync_baseline_files 内部 | per-repo `project_info_build:{hash}` 锁 | knowledge.py |
| **knowledge_migrate** | **`knowledge_migrate` 分布式锁, TTL 600s** | **db_setup.py** |

### 3.2 迁移函数异步执行期间的并发

迁移函数通过 `asyncio.to_thread()` + `asyncio.create_task()` 在 asyncio 事件循环中调度（非裸 threading.Thread），与项目里其他后台任务一致。通过 `acquire_lock("knowledge_migrate", ttl=600)` 分布式锁防止多实例并发。

Task 异常通过 `add_done_callback` 捕获并打印日志（`create_task` 异常默认静默）。Shutdown 时通过 `wait_for(timeout=10)` 尝试等待完成（不 cancel，因为迁移中断不如跑完）。

| 并发路径 | 现有保护 | 迁移期间是否安全 |
|---------|---------|--------------|
| maybe_refresh_project_info → _write_to_obs | `project_info_build` 锁 | 安全：查重改 name 后走 UPDATE 而非 INSERT |
| knowledge_write_worker → _do_write_knowledge | `knowledge_write` 维度锁 | 安全：去重用 repo_key+scope+agent_type+issue_num，新代码 obs_key 已用新格式 |
| knowledge_sync_loop | meta `knowledge_sync_running` 标记 | 安全：sync_loop 只处理 obs_key 为空的记录，迁移只处理 obs_key 非空的，记录集不重叠 |
| member 手动上传 | db_lock | 安全：新记录用新格式 obs_key + file_path |
| _delete_knowledge_full | db_lock | 安全：用 DB 中 obs_key 删除 OBS 对象 |
| events.py 删除知识 | db_lock | 安全：knowledge_storage_key() 生成新格式 obs_key |
| read_entry_content | 无锁（只读） | 安全：见 §3.2.1 时序分析 |
| _knowledge_cache | `_knowledge_cache_lock` | 安全：缓存 key 是 obs_key，迁移后旧 key 自然 miss |

### 3.2.1 迁移期间不需要 503 的时序分析

**步骤 1（baseline 去重 DELETE）**：
- DELETE 在 `db_lock` 内事务执行。并发读操作看到的是 commit 前的完整数据（隔离级别保证）。
- commit 后重复行被删除，保留的行数据完整。

**步骤 2（obs_key UPDATE + OBS 对象迁移）**：
- 时序：`save_bytes(new_key)` → `executemany UPDATE DB` → `commit` → `delete_object(old_key)`
- commit 前：DB 里 obs_key 仍是旧值，旧 OBS 对象还在 → 读请求走旧 key → 安全
- commit 后、删旧对象前：DB 里 obs_key 已是新值，新 OBS 对象已写入 → 读请求走新 key → 安全
- 删旧对象后：DB 已指向新 key → 安全
- **关键**：`read_bytes` 返回 None 时不加入 updates（不更新 DB、不删旧对象），下次启动重试

**步骤 3（file_path UPDATE）**：
- 纯 DB UPDATE，批量 commit。所有 `WORKSPACE_DIR + file_path` 路径拼接都有 `workspace_repo_dir()` 兜底。

### 3.3 与 Issue #67 去重的关系

Issue #67 按 `(repo_key, scope, agent_type, issue_num, file_path)` 五字段去重。Issue #190 按 `(repo_key, scope, name)` 去重（仅 baseline scope）。

Issue #67 的去重逻辑已同步修正：baseline 按 `(repo_key, scope, name)` 分组，其他 scope 仍按五字段分组。同时补充 scene_knowledge_ref 关联记录清理。

### 3.4 归档逻辑移除

归档机制（Issue #67 设计）已移除。原因：
- 手动归档接口（PUT /archive）前端 UI 已移除，无调用方
- Issue 关闭自动归档导致 agent 读不到知识（Issue reopen 后无法读取之前沉淀的知识）
- archived=0 维护成本高，20+ 处查询需记得加，漏加就出 bug
- issue_num 过滤已隔离不同 Issue 的知识，归档"避免干扰"的理由不成立

移除内容：
- 删除 `_archive_issue_knowledge()` 函数 + event_pipeline 调用
- 删除 `cleanup_archived_knowledge()` 函数 + main.py 定时任务
- 迁移函数步骤 0：`UPDATE archived=0 WHERE archived=1`（恢复现有归档记录）
- 迁移函数步骤 1 去重改回简单 MAX(id)（步骤 0 后无 archived=1 记录）

保留不动：
- `archived=0` 查询条件全部保留（所有记录都是 archived=0，查询自然命中）
- `archived` 字段保留在表结构中（向后兼容）
- `show_archived` 参数保留（无实际效果但不影响功能）

详见 `docs/weekly-03/issue-190-remove-archive.md`

## 4. 修复方案

### 4.1 统一 obs_key 生成函数

新增 `src/repo_key.py` 中的 `knowledge_obs_prefix(repo_key)`：

```python
def knowledge_obs_prefix(repo_key: str) -> str:
    provider, owner, repo = parse_repo_key(repo_key)
    return f"knowledge/{provider or 'gitcode'}/{owner}/{repo}"
```

替换所有 6 处 obs_key 生成点。member sync 路径从 file_path 提取 `member/` 或 `agent/` 后的相对路径（不用 replace，避免路径重复）。

### 4.2 baseline 去重改用 repo_key + name + archived=0

`builder.py` 的 `_write_to_obs()` 和 `knowledge.py` 的 `_sync_baseline_files()` 查重条件从 `file_path=?` 改为 `name=? AND archived=0`。

name 是固定中文展示名（"项目结构图谱"/"项目能力清单"/"项目元数据"），提取为 `BASELINE_NAME_MAP` / `BASELINE_DESC_MAP` 常量放在 `repo_key.py`（两个模块共享，避免跨层依赖和硬编码重复）。

### 4.3 全部 archived=0 补全

所有面向用户的查询和写入路径的查重都加 `archived=0`：

| 位置 | 用途 | 理由 |
|------|------|------|
| `load_project_info()` / `load_project_info_with_obs()` | 读 baseline | 归档记录不应读 |
| `_query_knowledge_obs()` | 读知识内容注入 prompt | 归档知识不应注入 |
| `_sync_baseline_files()` 查重 | baseline 去重 | 归档记录不应被 UPDATE |
| `builder.py _write_to_obs()` 查重 | baseline 去重 | 同上 |
| `_do_write_knowledge()` 内容去重 | 写入查重 | 新内容应创建新记录而非 UPDATE 归档记录 |
| `_do_write_knowledge()` INSERT 查重 | 写入查重 | 同上 |

不需要加 `archived=0` 的：rejected 查重（归档+rejected 都跳过）、disable refs（Issue 关闭时禁用所有含归档）、cleanup_archived_knowledge（专门查 archived=1）、events.py DELETE（删除操作）、list_knowledge（已有 show_archived 控制）。

### 4.4 全部 file_path 路径拼接兼容

所有 `os.path.join(WORKSPACE_DIR, file_path)` 都加 `workspace_repo_dir(repo_key)` 兜底：

| 位置 | 操作 | 兜底方式 |
|------|------|---------|
| `read_entry_content()` | 读文件 | 找不到时用 `workspace_repo_dir()` |
| `_update_knowledge_content()` | 写文件 | 目录不存在时用 `workspace_repo_dir()` |
| `_delete_knowledge_full()` | 删文件 | 找不到时用 `workspace_repo_dir()` |
| `cleanup_archived_knowledge()` | 删文件 | 找不到时用 `workspace_repo_dir()` |
| `knowledge_sync_loop()` | 读文件 | 已有 `workspace_repo_dir()` 兜底 |

### 4.5 删除废弃接口和死代码

删除：`GET /content`、`GET /context`、`DELETE /` query param、`_read_file()`、`_read_ws_file()`、`_safe_rel()`、`ALLOWED_EXTS`、`_member_docs()`、`_fmt_time()`、`_owner_repo()`（两处）、`MAX_UPLOAD_BYTES`。

### 4.6 list_member_docs kind/owner 从 DB 字段取

`_member_docs()` 已删除。删除前已修正：kind 从 `source` 字段判断（`manual` → member，`agent` → agent），owner 从 `created_by` 取。不依赖 file_path 格式。

### 4.7 数据清理迁移函数

#### 异步执行 + 分布式锁

通过 `asyncio.to_thread()` + `asyncio.create_task()` 调度（非裸 threading.Thread）。Task 异常用 `add_done_callback` 捕获。Shutdown 用 `wait_for(timeout=10)` 等待完成。

通过 `acquire_lock("knowledge_migrate", ttl=600)` 分布式锁防多实例并发。锁 key 和 meta key 都用功能语义命名（非 issue 号）。

#### 幂等控制 + 失败重试

通过 meta 表 `knowledge_migrate_done` 标记控制。**仅在所有步骤成功时才写标记**——每个步骤用 `_stepN_ok` 追踪，三个步骤都成功才写标记，否则下次启动重试。

#### 步骤 1：清理 baseline 重复记录

两阶段去重，**优先保留 archived=0 的记录**：
1. 先删归档记录（`archived=1` 且有同 `repo_key + name` 的 `archived=0` 记录存在）
2. 再按 `MAX(id)` 去重剩余记录

先删 scene_knowledge_ref 关联引用，再删 agent_shared_knowledge 主表行。MySQL 用临时表，SQLite 用子查询。

#### 步骤 2：更新 obs_key + OBS 对象迁移

obs_key 按 scope 分别处理（OLD_A 完全重写、OLD_B 插入 provider、member 提取相对路径）。

OBS 对象迁移流程：**读旧 key 内容 → 写新 key → 批量 UPDATE DB（executemany 单次 commit）→ 删旧 OBS 对象**。

关键安全逻辑：`read_bytes` 返回 None 或异常时 `continue`（不加入 updates，不更新 DB，不删旧对象），下次启动重试。旧对象删除失败只 warning 不阻断。

#### 步骤 3：更新 file_path

按格式分别处理（NEW 跳过、PARTIAL 补 provider、OLD 补完整前缀）。批量 executemany 单次 commit。

### 4.8 _sync_baseline_files OLD_B 格式检测

旧逻辑只检测 OLD_A（`startswith("project-info/")`），OLD_B（`knowledge/{owner}/{repo}/...` 缺 provider）会被跳过。改为用 `startswith(new_prefix)` 判断：不是新格式的都走写入逻辑重新写 OBS + UPDATE。

### 4.9 load_project_info 加 archived=0

`src/runtime/service.py` 两处 baseline 查询加 `AND archived=0`。

### 4.10 Issue #67 去重逻辑修正

baseline 按 `(repo_key, scope, name)` 分组（和 Issue #190 去重维度一致），其他 scope 仍按五字段分组。补充 scene_knowledge_ref 清理。

## 5. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/repo_key.py` | 新增 | `knowledge_obs_prefix()`、`BASELINE_NAME_MAP`、`BASELINE_DESC_MAP` |
| `src/project_info/builder.py` | 修改 | `_write_to_obs()` 查重改 name + archived=0 + obs_key 用新前缀 |
| `src/api/knowledge.py` | 修改 | `knowledge_storage_key()` 新前缀；`_sync_baseline_files()` 查重改 name + archived=0 + OLD_B 检测；sync member obs_key 修复路径重复；`read_entry_content()` + `cleanup_archived_knowledge()` 加 `workspace_repo_dir()` 兜底；`_do_write_knowledge()` 查重加 archived=0；删除死代码 `_owner_repo()` |
| `src/api/routes/knowledge.py` | 修改+删除 | member 上传 obs_key 新前缀；`_update_knowledge_content()` + `_delete_knowledge_full()` 加 `workspace_repo_dir()` 兜底；删除废弃接口 + 死代码（`_member_docs`、`_fmt_time`、`_owner_repo`、`_read_file`、`_read_ws_file`、`_safe_rel`、`ALLOWED_EXTS`、`MAX_UPLOAD_BYTES`） |
| `src/api/db_setup.py` | 新增+修改 | `_migrate_knowledge()`：分布式锁 + 两阶段去重（archived=0 优先）+ obs_key 归一化（OBS 对象迁移+删除，read 失败跳过）+ file_path 归一化；`_migrate_knowledge_issue67()` 去重修正 |
| `src/api/main.py` | 新增 | `asyncio.to_thread()` + `create_task()` + `add_done_callback` + shutdown `wait_for` |
| `src/runtime/service.py` | 修改 | `load_project_info()` + `load_project_info_with_obs()` 加 archived=0 |
| `tests/test_knowledge_board.py` | 修改 | 删除废弃接口测试，改为按 id 读知识详情 |
| `tests/test_openapi.py` | 修改 | 更新路由清单 |

## 6. 不受影响

- `load_project_info()`：dict 覆盖逻辑 + archived=0 过滤
- `knowledge_list` API：show_archived 参数控制
- `knowledge_stats` API：SQL 内已有 archived=0 条件
- `read_knowledge()`：按 obs_key 读取
- `member_files_pi()` / `agent_knowledge_files()`：通过 ref 表 JOIN
- prompt 注入：走 `load_project_info()`
- `_knowledge_cache`：obs_key 变更后旧 key 自然 miss

## 7. 回归测试要点

- baseline 知识库详情页：每个知识条目只出现一次
- project-info 刷新（webhook 触发）：不产生新重复记录
- OBS 读取：obs_key 更新后能正确读取内容
- 跨 provider 同名仓库：obs_key 不冲突
- 本地模式（OBS 未启用）：file_path 归一化后能正确读取文件
- member obs_key：upload 和 sync 路径格式一致，无路径重复
- 迁移幂等：多次启动不重复执行迁移
- 迁移期间并发 webhook：不产生新重复记录
- 迁移期间并发读取：所有路径都能读到有效数据（不需要 503）
- 迁移失败重试：步骤失败不写标记，下次启动重试
- OBS read 失败：跳过该记录，不更新 DB，不删旧对象
- 旧 OBS 对象清理：迁移后旧 key 对象被删除
- OLD_B 格式 obs_key：_sync_baseline_files 能检测并修正
- 归档 baseline 记录：去重时优先保留 archived=0 的记录

## 8. 确认点

1. baseline 去重维度用 repo_key + name + archived=0——确认
2. obs_key 新格式：`knowledge/{provider}/{owner}/{repo}/...`——确认
3. member obs_key 统一格式：`knowledge/{provider}/{owner}/{repo}/member/{username}/{filename}`——确认
4. member sync 路径 obs_key 从 file_path 提取 member/agent 相对路径（不用 replace）——确认
5. 数据清理两阶段去重：先删归档（有活跃同名的），再 MAX(id) 去重——确认
6. OBS 对象迁移：read → save → UPDATE DB → delete old，read 失败时跳过——确认
7. 迁移用 `asyncio.to_thread()` + `create_task()`，`add_done_callback` 捕获异常，shutdown `wait_for(10)`——确认
8. `acquire_lock("knowledge_migrate")` 分布式锁，meta key `knowledge_migrate_done`——确认
9. 所有 `WORKSPACE_DIR + file_path` 路径拼接都有 `workspace_repo_dir()` 兜底——确认
10. kind 从 source 字段判断，owner 从 created_by 取——确认
11. file_path 迁移按格式分别处理（OLD 补完整前缀、PARTIAL 只补 provider、NEW 跳过）——确认
12. obs_key 迁移按 scope 分别处理——确认
13. 迁移步骤失败不写标记，下次启动重试——确认
14. Issue #67 去重逻辑同步修正——确认
15. 所有面向用户的查询加 archived=0——确认
16. _sync_baseline_files 检测 OLD_B 格式（用 `startswith(new_prefix)` 判断）——确认
17. 归档逻辑移除（不归档、不清理、不 show_archived），迁移步骤 0 恢复 archived=1→0——确认
18. stats 查询和 list_knowledge 一致，均无 archived 过滤——确认
19. BASELINE_NAME_MAP / BASELINE_DESC_MAP 放在 repo_key.py 共享——确认
20. 函数/变量/日志用功能语义命名（非 issue 号）——确认

## 9. 上线后的 obs_key 和 file_path 格式

迁移完成后，所有 `agent_shared_knowledge` 记录的 obs_key 和 file_path 统一为新格式。

### 9.1 obs_key 格式

统一前缀：`knowledge/{provider}/{owner}/{repo}/...`

| scope | obs_key 格式 | 示例 |
|-------|-------------|------|
| baseline | `knowledge/{provider}/{owner}/{repo}/project-info/{filename}` | `knowledge/gitcode/grape-dev/grape/project-info/project-graph.md` |
| project | `knowledge/{provider}/{owner}/{repo}/project/knowledge.md` | `knowledge/gitcode/grape-dev/grape/project/knowledge.md` |
| issue | `knowledge/{provider}/{owner}/{repo}/issues/{n}/knowledge.md` | `knowledge/gitcode/grape-dev/grape/issues/42/knowledge.md` |
| issue_agent | `knowledge/{provider}/{owner}/{repo}/issues/{n}/{agent}/knowledge.md` | `knowledge/gitcode/grape-dev/grape/issues/42/coding/knowledge.md` |
| member（用户上传） | `knowledge/{provider}/{owner}/{repo}/member/{username}/{filename}` | `knowledge/gitcode/grape-dev/grape/member/xiaowu/entry_123.md` |
| member（Agent 生成） | `knowledge/{provider}/{owner}/{repo}/agent/{agent_type}/{filename}` | `knowledge/gitcode/grape-dev/grape/agent/coding/blog-config.json` |

- `provider`：`gitcode` 或 `github`，从 `parse_repo_key(repo_key)` 获取，默认 `gitcode`
- 跨 provider 同名仓库 obs_key 不同，不会互相覆盖
- 统一生成函数：`knowledge_obs_prefix(repo_key)` 返回 `knowledge/{provider}/{owner}/{repo}`，各调用方拼接后续路径

### 9.2 file_path 格式

统一格式：`{provider}/{owner}/{repo}/project-info/...`

| scope | file_path 格式 | 示例 |
|-------|---------------|------|
| baseline | `{provider}/{owner}/{repo}/project-info/{filename}` | `gitcode/grape-dev/grape/project-info/project-graph.md` |
| project | `{provider}/{owner}/{repo}/project-info/knowledge.md` | `gitcode/grape-dev/grape/project-info/knowledge.md` |
| issue | `{provider}/{owner}/{repo}/issues/{n}/knowledge.md` | `gitcode/grape-dev/grape/issues/42/knowledge.md` |
| issue_agent | `{provider}/{owner}/{repo}/issues/{n}/{agent}/knowledge.md` | `gitcode/grape-dev/grape/issues/42/coding/knowledge.md` |
| member（用户上传） | `{provider}/{owner}/{repo}/project-info/member/{username}/{filename}` | `gitcode/grape-dev/grape/project-info/member/xiaowu/entry_123.md` |
| member（Agent 生成） | `{provider}/{owner}/{repo}/project-info/agent/{agent_type}/{filename}` | `gitcode/grape-dev/grape/project-info/agent/coding/blog-config.json` |

- file_path 是 `WORKSPACE_DIR` 相对路径，`os.path.join(WORKSPACE_DIR, file_path)` 可直接拼出完整文件路径
- 旧格式兼容：`read_entry_content` / `_update_knowledge_content` / `_delete_knowledge_full` 等路径拼接处都有 `workspace_repo_dir()` 兜底

### 9.3 生成点一览

所有 obs_key 生成走 `knowledge_obs_prefix(repo_key)` 或 `knowledge_storage_key(repo_key, scope, agent_type, issue_num)`，无直接拼接：

| 生成点 | 函数 | 生成内容 |
|--------|------|---------|
| builder.py `_write_to_obs` | `knowledge_obs_prefix` | baseline obs_key |
| knowledge.py `knowledge_storage_key` | `knowledge_obs_prefix` | project/issue/issue_agent obs_key |
| knowledge.py sync member | `knowledge_obs_prefix` + 提取 member/agent 相对路径 | member obs_key |
| knowledge.py sync baseline | `knowledge_obs_prefix` | baseline obs_key |
| routes/knowledge.py upload | `knowledge_obs_prefix` | member obs_key |
| routes/events.py delete | `knowledge_storage_key` | project/issue/issue_agent obs_key |
| db_setup.py migrate | `knowledge_obs_prefix` / `knowledge_storage_key` | 迁移时生成新 key |
