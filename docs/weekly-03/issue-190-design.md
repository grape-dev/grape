# Issue #190 设计方案：项目知识库重复 + obs_key/file_path 统一修复

> Branch: fix/issue-190-duplicate-baseline (based on weekly-03-hc)
> Date: 2026-09-18 (updated after deep review)

## 1. 问题本质

`workspace_repo_dir()` 在某次迭代后路径增加了 provider 段（`gitcode/`、`github/`），但 `obs_key` 生成逻辑全部使用 `split_owner_repo()`（不含 provider）。多个问题叠加产生系统性缺陷。

### 1.1 file_path 三种历史格式

DB 中存在三种 file_path 格式：

| 格式 | 示例 | 说明 |
|------|------|------|
| OLD | `project-info/member/user/file.md` | 最早期，只有 project-info/ 前缀 |
| PARTIAL | `owner/repo/project-info/xxx.md` | 中期，有 owner/repo 但无 provider |
| NEW | `gitcode/owner/repo/project-info/xxx.md` | 当前代码写入，含 provider 前缀 |

影响：
- baseline 去重依赖 file_path → PARTIAL 和 NEW 格式的同一文件匹配不到 → 产生重复行
- `read_entry_content()` 本地兜底用 `os.path.join(WORKSPACE_DIR, file_path)` → OLD 格式路径在磁盘上不存在（实际在 `{WORKSPACE_DIR}/gitcode/owner/repo/...`）

### 1.2 obs_key 跨 provider 冲突

所有 obs_key 生成点用 `split_owner_repo()`，不含 provider 前缀：

```
gitcode:grape-dev/grape → knowledge/grape-dev/grape/project-info/xxx.md
github:grape-dev/grape  → knowledge/grape-dev/grape/project-info/xxx.md   ← 完全一样
```

同名仓库跨 provider 时 OBS 对象互相覆盖。

### 1.3 obs_key 三种历史格式 + member 路径重复

| 格式 | 示例 | 说明 |
|------|------|------|
| OLD_A | `project-info/gitcode:{owner}/{repo}/{fname}` | 最早期 baseline，不含 knowledge/ 前缀 |
| OLD_B | `knowledge/{owner}/{repo}/...` | 大多数，不含 provider |
| member 重复 | `knowledge/{owner}/{repo}/{owner}/{repo}/member/...` | sync 路径 replace 逻辑导致 owner/repo 重复 |

member 路径重复的根因（knowledge.py:954）：

```python
# file_path = mm6667/test/project-info/member/admin/entry.md
# replace('project-info/', '', 1) → mm6667/test/member/admin/entry.md
# storage_key = f"knowledge/{owner}/{rname}/{上面}" 
# = knowledge/mm6667/test/mm6667/test/member/admin/entry.md  ← mm6667/test 重复
```

### 1.4 member obs_key 生成路径不一致

| 路径 | 代码位置 | 生成格式 | 问题 |
|------|---------|---------|------|
| upload | routes/knowledge.py | `knowledge/{owner}/{repo}/member/{username}/{filename}` | 不含 project-info/ 中间段 |
| sync | knowledge.py | `knowledge/{owner}/{repo}/{file_path.replace('project-info/', '', 1)}` | file_path 含 owner/repo → replace 后仍含 → 路径重复 |

两个路径生成的 obs_key 不同，同一 member 文件在 upload 和 sync 后指向不同的 OBS 对象。

### 1.5 废弃接口（依赖 file_path，前端已无调用）

以下接口/函数依赖 file_path 做 WHERE 查询，但前端知识库页面统一用 id 操作（`GET /knowledge/{id}` 读内容、`DELETE /knowledge/{id}` 删除），这些接口无任何调用方，属于废弃代码：

| 废弃接口/函数 | 说明 |
|---|---|
| `GET /knowledge/content` `read_knowledge_content` | 前端走 `GET /knowledge/{id}` |
| `GET /knowledge/context` `knowledge_context` | 无任何调用方 |
| `DELETE /knowledge` (query param) `delete_knowledge` | 前端走 `DELETE /knowledge/{id}` |
| `_read_file()` | 只被 `read_knowledge_content` 调用，随接口一起删除 |
| `_read_ws_file()` | 无任何调用方 |
| `_member_docs()` | 唯一调用方 `knowledge_context` 已删除，变为死代码，一并删除 |
| `_fmt_time()` | 只被 `_member_docs` 本地模式调用，随 `_member_docs` 一起删除 |

删除后 knowledge 路由所有接口全部以 id 或 repo_key 为查询条件，没有任何接口再依赖 file_path 做 WHERE 查询。

### 1.6 DB 实际数据

baseline 重复记录（hello-skill，3 组 6 条）：

| id  | file_path 格式 | obs_key | created_by |
|-----|---------------|---------|------------|
| 153 | NEW (gitcode/...) | knowledge/xiaowu2119/hello-skill/... | system |
| 88  | PARTIAL (无 provider) | knowledge/xiaowu2119/hello-skill/... | sync |

project/issue/issue_agent：当前 0 条重复（去重用 repo_key+scope+agent_type+issue_num，不依赖 file_path）。

member：无去重逻辑，每次 INSERT，目前无重复。但 obs_key 有路径重复 bug（id=111/112）。

## 2. file_path 依赖点全梳理

知识库前端统一用 id 操作（`GET /knowledge/{id}` 读内容、`DELETE /knowledge/{id}` 删除），不走 file_path。

| 位置 | 用法 | 处理方式 |
|------|------|---------|
| builder.py `_write_to_obs()` | 去重 WHERE file_path=? | 改用 name 查重，不再依赖 file_path |
| knowledge.py `_sync_baseline_files()` | 去重 WHERE file_path=? | 改用 name 查重 |
| knowledge.py `read_entry_content()` | 本地兜底 `os.path.join(WORKSPACE_DIR, file_path)` | 加旧格式兼容（用 `workspace_repo_dir(repo_key)` 构建完整路径） |
| knowledge.py `member_files_pi()` | 从 file_path 提取 member/ 相对路径 | 安全：`if "project-info/" in file_path` 对三种格式均成立 |
| knowledge.py `knowledge_sync_loop()` | `os.path.join(WORKSPACE_DIR, file_path)` 读本地文件 | 迁移后 file_path 已归一化，安全。迁移期间旧格式可能读不到 → sync 跳过（`_sync_not_found`），不影响正确性 |
| routes/knowledge.py `list_member_docs()` | 已删除（死代码） | 随废弃接口一起删除 |
| routes/knowledge.py `read_knowledge_content` | WHERE file_path=? → obs_key | 废弃接口删除 |
| routes/knowledge.py `delete_knowledge` (query param) | WHERE file_path=? → 删 OBS + DB | 废弃接口删除 |
| routes/knowledge.py `_read_file()` | WHERE file_path=? → obs_key | 废弃函数删除 |
| routes/knowledge.py `_read_ws_file()` | WHERE file_path=? → obs_key | 废弃函数删除 |

## 3. 并发场景分析

### 3.1 现有锁一览

| 路径 | 锁 | 位置 |
|------|-----|------|
| maybe_refresh_project_info | `project_info_build:{md5(repo_key)[:16]}`, TTL 300s | builder.py |
| knowledge_write_worker | `knowledge_write:{md5(repo_key:scope:agent_type:issue_num)[:16]}`, TTL 60s | knowledge.py |
| knowledge_sync_loop | meta 表 `knowledge_sync_running` 标记 + 10 分钟超时 | knowledge.py |
| init_database (MySQL) | `GET_LOCK('grape_migrate', 300)` 跨实例互斥 | db_setup.py |
| _write_to_obs 内部 | `db_lock` | builder.py |
| _do_write_knowledge 内部 | `db_lock` + 内容去重 | knowledge.py |
| _sync_baseline_files 内部 | per-repo `project_info_build:{hash}` 锁 | knowledge.py |
| **_migrate_issue190** | **`knowledge_migrate` 分布式锁, TTL 600s** | **db_setup.py** |

### 3.2 迁移函数异步执行期间的并发

迁移函数用 daemon thread 异步执行，通过 `acquire_lock("knowledge_migrate", ttl=600)` 分布式锁防止多实例并发。迁移期间应用已 ready 接流量，以下并发路径可能同时执行。

| 并发路径 | 现有保护 | 迁移期间是否安全 |
|---------|---------|--------------|
| maybe_refresh_project_info → _write_to_obs | `project_info_build` 锁 | 安全：查重改 name 后走 UPDATE 而非 INSERT |
| knowledge_write_worker → _do_write_knowledge | `knowledge_write` 维度锁 | 安全：去重用 repo_key+scope+agent_type+issue_num，新代码 obs_key 已用新格式 |
| knowledge_sync_loop | meta `knowledge_sync_running` 标记 | 安全：sync_loop 只处理 obs_key 为空的记录，迁移只处理 obs_key 非空的，两者记录集不重叠 |
| member 手动上传 | db_lock | 安全：新记录用新格式 obs_key + file_path |
| _delete_knowledge_full | db_lock | 安全：用 DB 中 obs_key 删除 OBS 对象，始终匹配 |
| events.py 删除知识 | db_lock | 安全：knowledge_storage_key() 生成新格式 obs_key → delete_object；DB 记录被 DELETE → 无后续读取问题 |
| read_entry_content（并发读取） | 无锁（只读） | 安全：迁移批量 commit 前旧 obs_key 仍有效（旧 OBS 对象在 commit 后才删除）；file_path 旧格式走兼容逻辑兜底 |
| _knowledge_cache（内存缓存） | `_knowledge_cache_lock` | 安全：缓存 key 是 obs_key，迁移更新 obs_key 后旧缓存 key 自然 miss → 重新从 OBS 拉取 |

### 3.2.1 迁移期间 obs_key 不一致窗口

迁移 Step 2 批量 UPDATE obs_key（单次 commit），存在一个短暂窗口：commit 前所有记录的 DB obs_key 仍为旧值。此期间：

- _sync_baseline_files 查到记录（ORDER BY id DESC），obs_key 仍为旧格式 → 检测到非新格式 → 走写入逻辑重新写 OBS + UPDATE。迁移 commit 后 obs_key 更新为新格式，下次 sync 跳过。
- read_entry_content 用 DB 中的旧 obs_key 读取 OBS → 旧 OBS 对象尚未删除（删除在 commit 后）→ 读取成功。
- 批量 commit 将窗口缩短为单次事务（毫秒级）。
- 旧 OBS 对象在 DB commit 成功后删除，此时 DB 已指向新 key，旧 key 删除不影响读取。

### 3.3 与 Issue #67 去重的关系

Issue #67 按 `repo_key, scope, agent_type, issue_num, file_path` 五字段去重。Issue #190 按 `repo_key, name` 两字段去重（仅 baseline scope）。

- Issue #67 去不掉 file_path 不同但 name 相同的 baseline 重复 → Issue #190 补刀
- Issue #67 的去重逻辑已同步修正：baseline 按 `(repo_key, scope, name)` 去重，其他 scope 仍按五字段去重
- 两者顺序无关，各自幂等
- Issue #190 迁移异步执行，与 Issue #67 迁移（同步在 init_database 内）无执行顺序依赖

## 4. 修复方案

### 4.1 统一 obs_key 生成函数

新增 `src/repo_key.py` 中的 `knowledge_obs_prefix(repo_key)` 函数：

```python
def knowledge_obs_prefix(repo_key: str) -> str:
    """生成 OBS key 前缀：knowledge/{provider}/{owner}/{repo}"""
    provider, owner, repo = parse_repo_key(repo_key)
    return f"knowledge/{provider or 'gitcode'}/{owner}/{repo}"
```

替换所有 obs_key 生成点（6 处）：

| 文件 | 当前代码 | 改为 |
|------|---------|-----|
| builder.py | `f"knowledge/{owner}/{rname}/project-info/{name}"` | `f"{knowledge_obs_prefix(repo_key)}/project-info/{name}"` |
| knowledge.py `knowledge_storage_key()` | 用 `split_owner_repo` | 改用 `knowledge_obs_prefix(repo_key)` |
| knowledge.py sync member | `f"knowledge/{owner}/{rname}/{file_path.replace(...)}"` | 从 file_path 提取 member/ 或 agent/ 后的相对路径，拼接 `f"{knowledge_obs_prefix(repo_key)}/{rel}"` |
| knowledge.py sync baseline | `f"knowledge/{owner}/{rname}/project-info/{_fname}"` | `f"{knowledge_obs_prefix(repo_key)}/project-info/{_fname}"` |
| knowledge.py `_sync_baseline_files` | `f"knowledge/{_owner}/{_rname}/project-info/{fname}"` | `f"{knowledge_obs_prefix(repo_key)}/project-info/{fname}"` |
| routes/knowledge.py upload | `f"knowledge/{owner}/{rname}/member/{username}/{filename}"` | `f"{knowledge_obs_prefix(repo_key)}/member/{username}/{filename}"` |

member sync 路径的修复——不能用 replace，要从 file_path 中提取 `member/` 或 `agent/` 后的相对路径：

```python
if scope == "member":
    for marker in ("member/", "agent/"):
        idx = file_path.find(marker)
        if idx >= 0:
            rel = file_path[idx:]
            break
    else:
        rel = file_path
    storage_key = f"{knowledge_obs_prefix(repo_key)}/{rel}"
```

这样 upload 和 sync 两个路径生成的 member obs_key 格式统一为 `knowledge/{provider}/{owner}/{repo}/member/{username}/{filename}`。

### 4.2 baseline 去重改用 repo_key + name

`builder.py` 的 `_write_to_obs()` 和 `knowledge.py` 的 `_sync_baseline_files()` 查重条件都从 `file_path=?` 改为 `name=?`。

name 是固定中文展示名（"项目结构图谱"/"项目能力清单"/"项目元数据"），不受路径格式变化影响。repo_key 含 provider 前缀，天然区分跨 provider 同名仓库。查询和 UPDATE 都在现有 `db_lock` 内，并发安全。

Issue #67 的去重逻辑同步修正：baseline 按 `(repo_key, scope, name)` 分组保留 MAX(id)，其他 scope 仍按 `(repo_key, scope, agent_type, issue_num, file_path)` 分组。

### 4.3 删除废弃接口和死代码

删除以下废弃接口、函数和死代码：

- `GET /knowledge/content`（`read_knowledge_content`）
- `GET /knowledge/context`（`knowledge_context`）
- `DELETE /knowledge` query param 方式（`delete_knowledge`）
- `_read_file()`、`_read_ws_file()`、`_safe_rel()`、`ALLOWED_EXTS`
- `_member_docs()`（唯一调用方 `knowledge_context` 已删除，变为死代码）
- `_fmt_time()`（只被 `_member_docs` 调用）

保留：`DELETE /knowledge/{knowledge_id}`（前端实际使用）、`_update_knowledge_content`（被 `PUT /knowledge/{id}` 调用）。

### 4.4 read_entry_content() 本地兜底兼容

```python
if file_path:
    full = os.path.normpath(os.path.join(WORKSPACE_DIR, file_path))
    if os.path.isfile(full):
        with open(full, "r", encoding="utf-8") as f:
            return f.read()
    # 兼容旧格式 file_path（不含 provider/owner/repo 前缀）
    from src.repo_key import workspace_repo_dir
    alt = os.path.join(workspace_repo_dir(repo_key), file_path)
    if os.path.isfile(alt):
        with open(alt, "r", encoding="utf-8") as f:
            return f.read()
```

用 `workspace_repo_dir(repo_key)` 构建完整路径（含 provider/owner/repo），兼容 OLD 格式 file_path。迁移后旧格式 file_path 记录会被 UPDATE 为新格式，此兼容逻辑只做过渡期兜底。

### 4.5 list_member_docs kind/owner 从 DB 字段取

`_member_docs()` 已作为死代码删除。其逻辑（kind/owner 解析）在删除前已修正为从 DB 字段取：
- kind 从 `source` 字段判断：`source='manual'` → "member"，`source='agent'` → "agent"
- owner 从 `created_by` 字段取
- agent_type 字段在 scope='member' 时的含义是"关联的 agent 类型"，不是"文档由 agent 生成"

### 4.6 数据清理迁移函数

#### 异步执行 + 分布式锁

迁移函数放在 `src/api/main.py` 的 `lifespan()` 函数内，用 daemon thread 异步执行，不阻塞主流程应用启动。

通过 `acquire_lock("knowledge_migrate", ttl=600)` 分布式锁防止多实例并发执行。锁 key 用功能语义命名，和项目里 `knowledge_sync_running`、`project_info_build` 一致。

#### 幂等控制 + 失败重试

通过 meta 表 `issue190_migrate_done` 标记控制。**仅在所有步骤成功时才写标记**——每个步骤用 `_stepN_ok` 布尔值追踪成功/失败，三个步骤都成功才 `set_meta_value("issue190_migrate_done", "1")`，否则下次启动重试。

每个步骤自身幂等：

| 步骤 | 重复执行时的行为 | 幂等依据 |
|------|----------------|---------|
| baseline 去重 DELETE | 第二次没有重复行可删，DELETE 0 行 | 按 repo_key+name 分组保留 MAX(id)，已删除的 id 不在表中 |
| obs_key UPDATE | 旧 obs_key 已被改为新格式，`startswith(new_prefix)` 跳过 | 每条记录计算新 key 后先比较，相同则 skip |
| OBS 对象迁移 | OBS save_bytes 幂等（覆盖写），旧对象已删除则 read 返回空跳过 | 读取 → 写入 → 删除，每步独立幂等 |
| file_path UPDATE | 已是新格式的记录跳过 | 前缀判断跳过已归一化的记录 |

#### 步骤 1：清理 baseline 重复记录

按 `repo_key + name` 分组，保留 id 最大的一条，删除其余。先删 scene_knowledge_ref 中的关联引用，再删 agent_shared_knowledge 中的重复行。SQLite 和 MySQL 分别用子查询和临时表实现。

#### 步骤 2：更新 obs_key 为新格式（含 provider）+ OBS 对象迁移

obs_key 迁移按 scope 分别处理：

- **baseline / project / issue / issue_agent**（OLD_A 和 OLD_B 格式）：
  - OLD_A（`project-info/` 前缀）：完全重写为 `knowledge/{provider}/{owner}/{repo}/project-info/{fname}`
  - OLD_B（`knowledge/{owner}/{repo}/...` 缺 provider）：在 `knowledge/` 后插入 `provider/`
- **member**（可能含路径重复）：从旧 obs_key 提取 `member/` 或 `agent/` 后的相对路径，拼接新前缀

OBS 对象迁移流程：旧 key 读取内容 → 写入新 key → 批量 UPDATE DB（executemany 单次 commit）→ 删除旧 OBS 对象。旧 key 读不到内容时只 UPDATE DB obs_key，不删旧对象。旧对象删除失败只 warning 不阻断迁移。

#### 步骤 3：更新 file_path 为新格式

file_path 迁移按格式分别处理：

- NEW 格式（已含 provider/owner/repo）：跳过
- PARTIAL 格式（有 owner/repo 缺 provider）：只补 provider 前缀
- OLD 格式（只有 project-info/）：补完整 provider/owner/repo 前缀

批量 UPDATE（executemany 单次 commit）。

### 4.7 load_project_info 加 archived=0 过滤

`src/runtime/service.py` 的 `load_project_info()` 和 `load_project_info_with_obs()` 查询 baseline 记录时缺少 `archived=0` 条件。归档记录会被无谓地从 OBS 读取（dict 覆盖逻辑防止显示重复但浪费性能）。两处查询都加 `AND archived=0`。

### 4.8 Issue #67 去重逻辑修正

`db_setup.py` 的 `_migrate_knowledge_issue67()` 步骤 2 去重按 `(repo_key, scope, agent_type, issue_num, file_path)` 五字段分组。baseline scope 的重复记录因 file_path 格式不同查不出。

修正：baseline 按 `(repo_key, scope, name)` 分组，其他 scope 仍按五字段分组。同时补充 scene_knowledge_ref 关联记录清理（原代码只删主表不删 ref）。

## 5. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/repo_key.py` | 新增函数 | `knowledge_obs_prefix(repo_key)` |
| `src/project_info/builder.py` | 修改 | `_write_to_obs()` 查重改 name + obs_key 用新前缀 |
| `src/api/knowledge.py` | 修改 | `knowledge_storage_key()` 用新前缀；`_sync_baseline_files()` 查重改 name + obs_key 用新前缀 + OLD_B 格式检测；sync 循环 member obs_key 改为从 file_path 提取相对路径（修复路径重复）；`read_entry_content()` 本地兜底用 `workspace_repo_dir()` 兼容旧格式 |
| `src/api/routes/knowledge.py` | 修改+删除 | member 上传 obs_key 用新前缀；删除废弃接口 `GET /content`、`GET /context`、`DELETE /` query param；删除死代码 `_member_docs()`、`_fmt_time()`、`_read_file()`、`_read_ws_file()`、`_safe_rel()`、`ALLOWED_EXTS` |
| `src/api/db_setup.py` | 新增迁移函数 + 修改 | `_migrate_issue190()`：分布式锁 + 三步幂等迁移（baseline 去重 + obs_key 归一化含旧对象删除 + file_path 归一化）；`_migrate_knowledge_issue67()` 去重逻辑修正 |
| `src/api/main.py` | 新增调用 | lifespan() 内启动 `_migrate_issue190` daemon thread |
| `src/runtime/service.py` | 修改 | `load_project_info()` 和 `load_project_info_with_obs()` 加 `archived=0` 过滤 |
| `tests/test_knowledge_board.py` | 修改 | 删除废弃接口的测试用例，改为按 id 读知识详情 |
| `tests/test_openapi.py` | 修改 | 更新 openapi 路由清单，移除废弃接口，改为 knowledge_detail/knowledge_delete |

## 6. 不受影响

- `load_project_info()` / `load_project_info_with_obs()`：按 `os.path.basename(fp)` 做 dict key，重复记录会覆盖（last wins），obs_key 变更后读取路径自动更新。加 archived=0 后跳过归档记录。
- `knowledge_list` API：清理后无重复行，前端展示正常。
- `read_knowledge()`：按 scope + obs_key 读取，obs_key 更新后自动指向新对象。
- `agent_knowledge_files()` / `member_files_pi()`：通过 ref 表 JOIN 查询，走 `read_entry_content()` 读取内容，obs_key 更新后自动生效。
- `collect_knowledge_files()` / `generate_file_urls()`：使用 `load_project_info_with_obs()` 和 `member_files_pi()` 的返回值，obs_key 从 DB 读取，更新后自动生效。
- prompt 注入：走 `load_project_info()`，不受影响。
- `_knowledge_cache`：内存缓存 key 是 obs_key，迁移更新 obs_key 后旧 key 自然 miss。

## 7. 回归测试要点

- baseline 知识库详情页：每个知识条目只出现一次
- project-info 刷新（webhook 触发）：不产生新重复记录
- OBS 读取：obs_key 更新后能正确读取内容
- 跨 provider 同名仓库：obs_key 不冲突
- 本地模式（OBS 未启用）：file_path 归一化后能正确读取文件
- member obs_key：upload 和 sync 路径生成的格式一致，无路径重复
- 迁移幂等：多次启动不重复执行迁移
- 迁移期间并发 webhook：不产生新重复记录
- 迁移期间并发读取：read_entry_content 旧格式兼容生效
- 迁移失败重试：步骤失败不写标记，下次启动重试
- 旧 OBS 对象清理：迁移后旧 key 对象被删除
- OLD_B 格式 obs_key：_sync_baseline_files 能检测并修正

## 8. 确认点

1. baseline 去重维度用 repo_key + name（而非 file_path）——确认
2. obs_key 新格式：`knowledge/{provider}/{owner}/{repo}/...`——确认
3. member obs_key 统一格式：`knowledge/{provider}/{owner}/{repo}/member/{username}/{filename}`——确认
4. member sync 路径 obs_key 生成从 file_path 提取 member/agent 相对路径（不用 replace）——确认
5. 数据清理保留 id 最大的一条（新格式记录）——确认
6. OBS 旧对象在 DB commit 后删除，不残留——确认
7. 迁移函数用 daemon thread 异步执行，`acquire_lock("knowledge_migrate")` 分布式锁防多实例并发——确认
8. `read_entry_content()` 本地兜底用 `workspace_repo_dir()` 构建完整路径——确认
9. kind 从 source 字段判断，owner 从 created_by 取——确认
10. file_path 迁移按格式分别处理（OLD 补完整前缀、PARTIAL 只补 provider、NEW 跳过）——确认
11. obs_key 迁移按 scope 分别处理（baseline/project/issue/issue_agent insert provider，member 提取 member/agent 相对路径重新拼接）——确认
12. 迁移步骤失败不写标记，下次启动重试——确认
13. Issue #67 去重逻辑同步修正（baseline 按 name，其他 scope 仍按 file_path）——确认
14. load_project_info 加 archived=0 过滤——确认
15. _sync_baseline_files 检测 OLD_B 格式（用 `startswith(new_prefix)` 判断，非旧逻辑的 `startswith("project-info/")`）——确认
