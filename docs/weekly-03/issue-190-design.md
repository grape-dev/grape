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

name 是固定中文展示名（"项目结构图谱"/"项目能力清单"/"项目元数据"），提取为 `BASELINE_NAME_MAP` / `BASELINE_DESC_MAP` 常量放在 `repo_key.py`（两个模块共享，避免跨层依赖和硬编码重复）。fallback 值 `BASELINE_NAME_FALLBACK` / `BASELINE_DESC_FALLBACK` 同在 `repo_key.py`。

name 自动生成逻辑统一抽取为 `derive_knowledge_name()`（`src/api/knowledge.py`），按 scope 规则推导：baseline 用 BASELINE_NAME_MAP 精确映射、member 用 file_path 文件名去扩展名、project/issue/issue_agent 用 "项目知识 - {Agent}" / "Issue #{n} 知识" 等。写入时自动生成和存量数据回填补偿共用同一函数，避免多处重复。

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

不需要加 `archived=0` 的：rejected 查重（查到已存在记录就跳过，归档了也跳过，行为正确）、disable refs（Issue 关闭时禁用所有含归档）、cleanup_archived_knowledge（已删除）、events.py DELETE（删除操作）、list_knowledge（已删 show_archived）、stats（已删 archived=0）。

> **注**：归档逻辑移除后所有记录 archived=0，保留的 `archived=0` 条件永远为 true，不影响功能。rejected 分支不需要加——查到已存在记录就跳过，归档与否行为都正确。

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

迁移分两个函数，执行时机不同：

#### _migrate_knowledge_dedup_and_ref()（每次启动执行，幂等）

在 `init_database()` 中同步调用。步骤顺序：
1. source 回填（空值 → system/agent/manual）
2. name/description 回填（所有 scope，用 `derive_knowledge_name()` 推导，必须在去重之前）
3. baseline 去重（按 repo_key + name，派生表子查询，SQLite/MySQL 通用）+ 其他 scope 去重
4. scene_knowledge_ref 数据迁移（INSERT OR IGNORE）
5. 孤儿 scene_knowledge_ref 清理（knowledge_id 指向已删除记录）

issue95 修复部分有独立幂等标记 `issue95_migrate_done`。

#### _migrate_knowledge()（首次执行，受 knowledge_migrate_done 标记控制）

在 `lifespan()` 中异步执行（`asyncio.to_thread` + `create_task`）。仅执行一次：
1. 恢复归档记录（archived=1 → 0）
2. obs_key 归一化（OLD_A/OLD_B → 新格式含 provider，OBS 对象迁移 read→save→UPDATE→delete old）
3. file_path 归一化（OLD/PARTIAL → 新格式含 provider）

> baseline 去重和 name/description 回填已移至 `_migrate_knowledge_dedup_and_ref()`（每次启动执行），此处不再重复。

### 4.8 _sync_baseline_files OLD_B 格式检测

旧逻辑只检测 OLD_A（`startswith("project-info/")`），OLD_B（`knowledge/{owner}/{repo}/...` 缺 provider）会被跳过。改为用 `startswith(new_prefix)` 判断：不是新格式的都走写入逻辑重新写 OBS + UPDATE。

### 4.9 load_project_info 加 archived=0

`src/runtime/service.py` 两处 baseline 查询加 `AND archived=0`。

### 4.10 Issue #67 去重逻辑修正

baseline 按 `(repo_key, scope, name)` 分组（和 Issue #190 去重维度一致），其他 scope 仍按五字段分组。补充 scene_knowledge_ref 清理。

## 5. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/repo_key.py` | 新增 | `knowledge_obs_prefix()`、`BASELINE_NAME_MAP`、`BASELINE_DESC_MAP`、`BASELINE_NAME_FALLBACK`、`BASELINE_DESC_FALLBACK` |
| `src/localfs.py` | 新增 | 原子文件操作模块：`write_text`/`write_bytes`（tmp+fsync+replace+fsync dir）、`read_text`/`read_bytes`、`append_text`、`safe_join`/`safe_workspace_path` |
| `src/api/knowledge.py` | 新增+修改 | `derive_knowledge_name()` 统一 name 推导；`knowledge_storage_key()` 新前缀；`_sync_baseline_files()` 查重改 name + 精确映射；sync_loop UPDATE 补 name/description（嵌套 NULLIF 覆盖统一值）；rejected INSERT 补 name+description + 查重加 name；`read_entry_content()` 加兜底；`_do_write_knowledge` 追加写入改原子 read+write；删除死代码 |
| `src/project_info/builder.py` | 修改 | `_write_to_obs()` 查重改 name + archived=0 + obs_key 新前缀 + description 用 BASELINE_DESC_MAP + fallback 用常量 + open("w") 改 localfs.write_text |
| `src/api/routes/knowledge.py` | 修改+删除 | member 上传 obs_key 新前缀；2 处 open("w") 改 localfs.write_text；删除废弃接口 + 死代码 |
| `src/api/db_setup.py` | 新增+修改 | `_migrate_knowledge_dedup_and_ref()`：source 回填 + name/description 回填（所有 scope，derive_knowledge_name）+ 派生表去重 + 孤儿 ref 清理；`_migrate_knowledge()`：恢复归档 + obs_key 归一化 + file_path 归一化（dedup 已移除） |
| `src/api/main.py` | 修改 | `asyncio.to_thread()` + `create_task()` + `add_done_callback` + shutdown `wait_for` |
| `src/api/routes/events.py` | 修改 | 删除知识时从 DB 取 obs_key（不用 knowledge_storage_key 重新生成） |
| `src/runtime/service.py` | 修改 | `load_project_info()` + `load_project_info_with_obs()` 加 archived=0 |
| `tests/test_issue190_knowledge_dedup.py` | 新增 | 30 个测试：obs_key 格式、name 映射、迁移幂等、回填+去重集成、孤儿 ref 清理 |
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
19. BASELINE_NAME_MAP / BASELINE_DESC_MAP / FALLBACK 常量放在 repo_key.py，derive_knowledge_name() 放在 knowledge.py——确认
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

> **历史遗留**：DB 中可能存在少量 `knowledge/{provider}/{owner}/{repo}/agent/{agent_type}/{filename}` 格式的 obs_key（旧代码 Agent 生成文档写到 `project-info/agent/` 目录）。当前代码已统一为 `member/` 目录，不再产生新的 `agent/` 路径。`member_files_pi` 读取时自动将 `agent/` 归一化为 `member/`。

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

> **历史遗留**：同 obs_key，file_path 中也可能存在 `agent/{agent_type}/` 格式的旧数据，读取时自动归一化为 `member/`。

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
| routes/events.py delete | 从 DB 取实际 obs_key | project/issue/issue_agent obs_key |
| db_setup.py migrate | `knowledge_obs_prefix` / `knowledge_storage_key` | 迁移时生成新 key |

## 10. 审查驱动的后续修复

以下修复在初始设计完成后由外部 AI 审查驱动，逐轮发现并修复。

### 10.1 name/description 回填移至每次启动路径

**问题**：回填步骤原在 `_migrate_knowledge()`（受 `knowledge_migrate_done=1` 标记控制），已跑过旧迁移的环境永远不会执行回填。而 `_migrate_knowledge_dedup_and_ref()` 每次启动执行去重——去重按 `(repo_key, name)` GROUP BY，如果 name 为空或统一值，同 repo 的 3 条 baseline 会被合并为 1 条，丢失 2 条。

**修复**：回填从 `_migrate_knowledge()` 移到 `_migrate_knowledge_dedup_and_ref()` step 1.5（source 回填之后、去重之前）。每次启动执行，幂等（COALESCE 空值不覆盖，统一值匹配时替换）。

### 10.2 description 按文件区分

**问题**：builder `_write_to_obs` 硬编码 `description="codegraph 自动生成的项目基线知识"`，三个不同文件用同一个注释，用户在列表页无法区分。

**修复**：改为 `BASELINE_DESC_MAP.get(name, BASELINE_DESC_FALLBACK)`，每个文件有独立描述。迁移回填也同步修正统一 description。

### 10.3 fallback 常量统一

**问题**：builder / _sync_baseline_files / write_knowledge / 迁移函数各自硬编码 fallback 值，且不一致（builder 用 "codegraph 自动生成的项目基线知识"，sync 用 "VOD 基线分析自动生成"，查重用 fname，INSERT 用 "项目基线分析"）。

**修复**：`repo_key.py` 新增 `BASELINE_NAME_FALLBACK` 和 `BASELINE_DESC_FALLBACK`，所有引用点统一。

### 10.4 rejected INSERT 补 name+description

**问题**：`_do_write_knowledge` rejected 分支的 INSERT 语句字段列表没有 name 和 description，写入后 name = DEFAULT ''。

**修复**：补上 name + description 字段。5 个 INSERT 路径全部包含这两个字段（AST 扫描确认）。

### 10.5 sync UPDATE 路径补 description

**问题**：`knowledge_sync_loop` 和 `_sync_baseline_files` 的 UPDATE 路径只补 obs_key/content_hash/source，不碰 name/description。

**修复**：两处 UPDATE 加 `name=COALESCE(NULLIF(name, ''), ?)` 和 `description=COALESCE(NULLIF(description, ''), ?)`，空值回填不覆盖已有值。

### 10.6 孤儿 scene_knowledge_ref 清理

**问题**：历史迁移轮次残留 53 条孤儿 ref（knowledge_id 指向已删除的知识记录）。

**修复**：`_migrate_knowledge_dedup_and_ref()` 加 step 5，每次启动检查并清理 `WHERE knowledge_id NOT IN (SELECT id FROM agent_shared_knowledge)`。

### 10.7 MySQL/SQLite 去重 SQL 统一

**问题**：去重用 `DELETE ... NOT IN (SELECT MAX(id) FROM same_table)` 在 MySQL 报 "can't specify target table for update in FROM clause"。分了两套代码（MySQL 用 TEMPORARY TABLE，SQLite 用直接子查询）。

**修复**：用标准派生表子查询 `NOT IN (SELECT keep.keep_id FROM (SELECT MAX(id) ...) AS keep)`，SQLite/MySQL 通用，删了 ~45 行重复代码。

### 10.8 本地文件原子写入

**问题**：6 处 `open(path, "w")` 非原子，写入中途崩溃会截断目标文件。

**修复**：新建 `src/localfs.py` 模块，封装：
- `write_text` / `write_bytes`：同目录临时文件 + flush + fsync(fd) + os.replace + fsync(dir)
- `read_text` / `read_bytes`：安全读取
- `append_text`：非原子追加
- `safe_join` / `safe_workspace_path`：路径穿越校验

6 处 open("w") 全部替换为 `localfs.write_text`。

### 10.9 sync_loop 精确映射替代 FALLBACK

**问题**：`knowledge_sync_loop` UPDATE 对 baseline 传 `BASELINE_NAME_FALLBACK` 而非按 file_path 精确映射。

**修复**：baseline 分支已有 `_fname = os.path.basename(file_path)`，直接用它查 `BASELINE_NAME_MAP` / `BASELINE_DESC_MAP`，传精确值给 COALESCE。

### 10.10 COALESCE 嵌套覆盖统一 description

**问题**：`_sync_baseline_files` UPDATE 用 `COALESCE(NULLIF(description, ''), ?)`，统一值 "codegraph 自动生成的项目基线知识" 非空，不会被替换。

**修复**：嵌套 NULLIF：`COALESCE(NULLIF(NULLIF(NULLIF(desc, ''), unified), fallback), ?)`，空值、统一值、fallback 值都转为 NULL 后用精确值替换。

### 10.11 rejected 查重加 name 条件

**问题**：`_do_write_knowledge` rejected 分支查重用 `(repo_key, scope, agent_type, issue_num)`，baseline scope 同 repo 的 3 条不同文件会匹配到同一条已有记录，后续 rejected 记录被跳过。

**修复**：查重加 `AND name=?`。

### 10.12 追加写入改原子

**问题**：`_do_write_knowledge` 用 `open("a")` 追加写入，崩溃时可能截断已有内容。

**修复**：改为 `localfs.read_text` + `localfs.write_text`（原子替换）。安全因为有分布式锁保护同维度串行。

### 10.13 derive_knowledge_name 抽取

**问题**：name 自动生成逻辑在 write_knowledge、迁移 step 1.5、step 1.6 三处重复，且 member/issue/project 的规则各自硬编码。

**修复**：抽取为 `derive_knowledge_name(scope, agent_type, issue_num, file_path)` 放在 `src/api/knowledge.py`（知识层逻辑）。三处引用统一调此函数。step 1.5 + 1.6 合并为一个统一回填步骤，查所有 scope 的空 name / fallback name / 统一 description。

### 10.14 _migrate_knowledge 冗余 dedup 删除

**问题**：`_migrate_knowledge` 里有 baseline dedup 步骤，与 `_migrate_knowledge_dedup_and_ref` 的 step 2 重复（后者每次启动执行）。

**修复**：删除 `_migrate_knowledge` 里的 dedup 步骤和 `_dedup_ok` 变量。`_migrate_knowledge` 现在只做：恢复归档 → obs_key 归一化 → file_path 归一化。

### 10.15 normal-path 查重加 name 条件

**问题**：`_do_write_knowledge` normal 路径（非 rejected）查重用 `(repo_key, scope, agent_type, issue_num)`，不含 name。baseline scope（agent_type='', issue_num=0）的 3 个不同文件会匹配同一条记录互相覆盖。

**修复**：加 `AND name=?`，和 rejected 路径一致。

### 10.16 derive_knowledge_description 抽取 + 非 baseline description 回填

**问题**：回填 `_needs_desc` 只对 baseline scope 设 True，非 baseline（project/issue/issue_agent）的空 description 永远不会被回填。write_knowledge 里 description 生成逻辑也和迁移重复。

**修复**：抽取 `derive_knowledge_description(scope, agent_type, issue_num, file_path)` 放在 `src/api/knowledge.py`，和 `derive_knowledge_name` 对称。write_knowledge 和迁移回填共用。回填现在对所有 scope 生成 description。

### 10.17 source=manual 记录不回填 description

**问题**：回填对 member scope 记录生成 description 时，derive_knowledge_description("member") 返回空字符串，隐式跳过。但如果 member name 为空需要回填 name 时，UPDATE 会把 description 也设为空字符串——清空用户已填的 description。

**修复**：基于 source 字段判断——source="manual" 的记录不碰 description，保持用户填的原值。只有 system/agent 记录才回填 description。

### 10.18 孤儿 ref 清理用 EXISTS 替代 COUNT

**问题**：孤儿 ref 清理用 `SELECT COUNT(*) ... NOT IN (SELECT id ...)` 全表扫描计数。

**修复**：改为 `SELECT EXISTS(SELECT 1 ...)`，命中第一行即返回，无孤儿时跳过 DELETE。

### 10.19 localfs.write_text 加 fsync 目录

**问题**：os.replace 后如果不 fsync 目录，掉电时 rename 元数据可能未落盘，文件名存在但 inode 丢失。

**修复**：os.replace 后 `os.open(dir, O_RDONLY)` → `os.fsync(dir_fd)` → `os.close(dir_fd)`，OSError 兜底防 tmpfs 等不支持目录 fsync 的文件系统。

### 10.20 localfs 注释修正

**问题**：docstring 写"原子写入文本文件"，不准确——只有 os.replace 是原子的，写入本身不是。

**修复**：改为"同目录临时文件 + os.replace 原子替换目标文件，避免进程崩溃时目标文件处于半写/截断状态"。
