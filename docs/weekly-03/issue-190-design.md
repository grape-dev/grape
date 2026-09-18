# Issue #190 设计方案：项目知识库重复 + obs_key/file_path 统一修复

> Branch: fix/issue-190-duplicate-baseline (based on weekly-03-hc)
> Date: 2026-09-18

## 1. 问题本质

`workspace_repo_dir()` 在某次迭代后路径增加了 provider 段（`gitcode/`、`github/`），但 `obs_key` 生成逻辑全部使用 `split_owner_repo()`（不含 provider）。多个问题叠加产生系统性缺陷。

### 1.1 file_path 三种历史格式

DB 中存在三种 file_path 格式：

| 格式 | 示例 | 记录数 | 说明 |
|------|------|--------|------|
| OLD | `project-info/member/user/file.md` | 5 | 最早期，只有 project-info/ 前缀 |
| PARTIAL | `owner/repo/project-info/xxx.md` | 22 | 中期，有 owner/repo 但无 provider |
| NEW | `gitcode/owner/repo/project-info/xxx.md` | 3 | 当前代码写入，含 provider 前缀 |

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

DB 中存在三种 obs_key 格式：

| 格式 | 示例 | 说明 |
|------|------|------|
| OLD_A | `project-info/gitcode:{owner}/{repo}/{fname}` | 最早期 baseline，不含 knowledge/ 前缀 |
| OLD_B | `knowledge/{owner}/{repo}/...` | 大多数，不含 provider |
| member 重复 | `knowledge/{owner}/{repo}/{owner}/{repo}/member/...` | sync 路径 replace 逻辑导致 owner/repo 重复 |

OBS_PREFIX（如 `grape/prod`）由 ObsStorage `_full()` 方法自动拼接，DB 中 obs_key 是业务 key 不含 OBS_PREFIX。确认 DB 中无含 OBS_PREFIX 的记录（0 条）。

member 路径重复的根因（knowledge.py:954）：

```python
# file_path = mm6667/test/project-info/member/admin/entry.md
# replace('project-info/', '', 1) → mm6667/test/member/admin/entry.md
# storage_key = f"knowledge/{owner}/{rname}/{上面}" 
# = knowledge/mm6667/test/mm6667/test/member/admin/entry.md  ← mm6667/test 重复
```

### 1.4 member obs_key 生成路径不一致

member 的 obs_key 在两个代码路径生成的格式不同：

| 路径 | 代码位置 | 生成格式 | 问题 |
|------|---------|---------|------|
| upload | routes/knowledge.py:745 | `knowledge/{owner}/{repo}/member/{username}/{filename}` | 不含 project-info/ 中间段 |
| sync | knowledge.py:954 | `knowledge/{owner}/{repo}/{file_path.replace('project-info/', '', 1)}` | file_path 含 owner/repo → replace 后仍含 → 路径重复 |

两个路径生成的 obs_key 不同，同一 member 文件在 upload 和 sync 后指向不同的 OBS 对象。

### 1.5 list_member_docs() 解析逻辑不兼容 PARTIAL/NEW 格式

routes/knowledge.py:187 从 file_path 解析 kind/owner：

```python
parts = file_path.replace("\\", "/").split("/")
if len(parts) >= 4 and parts[0] == "project-info":  # ← 固定取 parts[0]
    kind = parts[1]
    owner = parts[2]
```

OLD 格式 `project-info/member/user/file.md`：parts[0] == "project-info" ✓
PARTIAL 格式 `owner/repo/project-info/member/user/file.md`：parts[0] == "owner" ✗（当前已有 bug）
NEW 格式 `gitcode/owner/repo/project-info/member/user/file.md`：parts[0] == "gitcode" ✗

PARTIAL 和 NEW 格式都会走 else 分支，kind/owner 解析错误。这是 workspace_repo_dir 加 provider 前缀后就已存在的问题，不是迁移引入的。

### 1.6 废弃接口（依赖 file_path，前端已无调用）

以下接口/函数依赖 file_path 做 WHERE 查询，但前端知识库页面统一用 id 操作（`GET /knowledge/{id}` 读内容、`DELETE /knowledge/{id}` 删除），这些接口无任何调用方，属于废弃代码：

| 废弃接口/函数 | 位置 | 说明 |
|---|---|---|
| `GET /knowledge/content` | routes/knowledge.py:394 `read_knowledge_content` | 前端走 `GET /knowledge/{id}` |
| `GET /knowledge/context` | routes/knowledge.py:428 `knowledge_context` | 无任何调用方 |
| `DELETE /knowledge` (query param) | routes/knowledge.py:459 `delete_knowledge` | 前端走 `DELETE /knowledge/{id}` |
| `_read_file()` | routes/knowledge.py:72 | 只被 `read_knowledge_content` 调用，随接口一起删除 |
| `_read_ws_file()` | routes/knowledge.py:110 | 无任何调用方 |

本 Issue 一并删除，减少 file_path 依赖面。`_safe_rel()` 和 `ALLOWED_EXTS` 如果只被这些废弃函数使用也一并清理。

删除后 knowledge 路由所有接口全部以 id 或 repo_key 为查询条件，没有任何接口再依赖 file_path 做 WHERE 查询。

### 1.7 DB 实际数据

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
| builder.py:149 `_write_to_obs()` | 去重 WHERE file_path=? | 改用 name 查重，不再依赖 file_path |
| knowledge.py:1063 `_sync_baseline_files()` | 去重 WHERE file_path=? | 改用 name 查重 |
| knowledge.py:92 `read_entry_content()` | 本地兜底 `os.path.join(WORKSPACE_DIR, file_path)` | 加旧格式兼容 |
| knowledge.py:863 `member_files_pi()` | 从 file_path 提取 member/ 相对路径 | 安全：`if "project-info/" in file_path` 对三种格式均成立 |
| knowledge.py:937 `knowledge_sync_loop()` | `os.path.join(WORKSPACE_DIR, file_path)` 读本地文件 | 迁移后 file_path 已归一化，安全。迁移期间旧格式可能读不到 → sync 跳过（`_sync_not_found`），不影响正确性 |
| routes/knowledge.py:187 `list_member_docs()` | 解析 file_path 路径段提取 kind/owner | **需修复**：当前逻辑 `parts[0]=="project-info"` 对 PARTIAL/NEW 格式解析错误 |
| routes/knowledge.py:394 `read_knowledge_content` | WHERE file_path=? → obs_key | **废弃接口删除**：前端走 `GET /knowledge/{id}` |
| routes/knowledge.py:459 `delete_knowledge` (query param) | WHERE file_path=? → 删 OBS + DB | **废弃接口删除**：前端走 `DELETE /knowledge/{id}` |
| routes/knowledge.py:72 `_read_file()` | WHERE file_path=? → obs_key | **废弃函数删除**：只被 `read_knowledge_content` 调用 |
| routes/knowledge.py:110 `_read_ws_file()` | WHERE file_path=? → obs_key | **废弃函数删除**：无任何调用方 |

## 3. 并发场景分析

### 3.1 现有锁一览

| 路径 | 锁 | 位置 |
|------|-----|------|
| maybe_refresh_project_info | `project_info_build:{md5(repo_key)[:16]}`, TTL 300s | builder.py:433 |
| knowledge_write_worker | `knowledge_write:{md5(repo_key:scope:agent_type:issue_num)[:16]}`, TTL 60s | knowledge.py:303 |
| knowledge_sync_loop | meta 表 `knowledge_sync_running` 标记 + 10 分钟超时 | knowledge.py:896 |
| init_database (MySQL) | `GET_LOCK('grape_migrate', 300)` 跨实例互斥 | db_setup.py:1425 |
| _write_to_obs 内部 | `db_lock` | builder.py:144 |
| _do_write_knowledge 内部 | `db_lock` + 内容去重 | knowledge.py:384 |
| _sync_baseline_files 内部 | per-repo `project_info_build:{hash}` 锁 | knowledge.py:1039 |

### 3.2 迁移函数异步执行期间的并发

迁移函数用 daemon thread 异步执行，不持有 `GET_LOCK('grape_migrate')`。迁移期间应用已 ready 接流量，以下并发路径可能同时执行。迁移设计为最终一致性：迁移期间旧格式数据仍可用，迁移完成后自动切换到新格式。

| 并发路径 | 现有保护 | 迁移期间是否安全 |
|---------|---------|--------------|
| maybe_refresh_project_info → _write_to_obs | `project_info_build` 锁 | 安全：查重改 name 后走 UPDATE 而非 INSERT |
| knowledge_write_worker → _do_write_knowledge | `knowledge_write` 维度锁 | 安全：去重用 repo_key+scope+agent_type+issue_num，新代码 obs_key 已用新格式 |
| knowledge_sync_loop | meta `knowledge_sync_running` 标记 | 安全：迁移后行 obs_key 已非空，sync_loop 不处理；sync_loop 内 obs_key 生成也改为新格式 |
| member 手动上传 | db_lock | 安全：新记录用新格式 obs_key + file_path |
| _delete_knowledge_full（routes/knowledge.py:676） | db_lock | 安全：用 DB 中 obs_key 删除 OBS 对象，始终匹配 |
| events.py:1306 删除知识 | db_lock | 安全：knowledge_storage_key() 生成新格式 obs_key → delete_object；旧 key 对象残留（方案设计不删除旧对象）；DB 记录被 DELETE → 无后续读取问题 |
| read_entry_content（并发读取） | 无锁（只读） | 安全：迁移期间旧 obs_key 仍指向有效 OBS 对象（不删除旧对象）；file_path 旧格式走兼容逻辑兜底 |
| _knowledge_cache（内存缓存） | `_knowledge_cache_lock` | 安全：缓存 key 是 obs_key，迁移更新 obs_key 后旧缓存 key 自然 miss → 重新从 OBS 拉取 |

结论：现有锁机制已覆盖所有并发路径，不需要新增锁。最终一致性保证：迁移完成后所有记录统一为新格式，迁移期间旧格式仍可用。

### 3.3 与 Issue #67 去重的关系

Issue #67 按 `repo_key, scope, agent_type, issue_num, file_path` 五字段去重。Issue #190 按 `repo_key, name` 两字段去重（仅 baseline scope）。

- Issue #67 去不掉 file_path 不同但 name 相同的 baseline 重复 → Issue #190 补刀
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

| 文件 | 行 | 当前代码 | 改为 |
|------|---|---------|-----|
| builder.py:140 | `f"knowledge/{owner}/{rname}/project-info/{name}"` | `f"{knowledge_obs_prefix(repo_key)}/project-info/{name}"` |
| knowledge.py:133-152 | `knowledge_storage_key()` 用 `split_owner_repo` | 改用 `knowledge_obs_prefix(repo_key)` |
| knowledge.py:954 | `f"knowledge/{owner}/{rname}/{file_path.replace('project-info/', '', 1)}"` | 从 file_path 提取 member/ 或 agent/ 后的相对路径，拼接 `f"{knowledge_obs_prefix(repo_key)}/{rel}"` |
| knowledge.py:958 | `f"knowledge/{owner}/{rname}/project-info/{_fname}"` | `f"{knowledge_obs_prefix(repo_key)}/project-info/{_fname}"` |
| knowledge.py:1083 | `f"knowledge/{_owner}/{_rname}/project-info/{fname}"` | `f"{knowledge_obs_prefix(repo_key)}/project-info/{fname}"` |
| routes/knowledge.py:745 | `f"knowledge/{owner}/{rname}/member/{username}/{filename}"` | `f"{knowledge_obs_prefix(repo_key)}/member/{username}/{filename}"` |

member sync 路径（knowledge.py:954）的修复需要特殊处理——不能用 replace，要从 file_path 中提取 `member/` 或 `agent/` 后的相对路径：

```python
# 修复后 (knowledge.py:954 member sync)
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

`builder.py` 的 `_write_to_obs()` 查重条件：

```python
# 当前（有 bug）：
"WHERE repo_key=*** AND scope='baseline' AND file_path=?"

# 改为：
"WHERE repo_key=*** AND scope='baseline' AND name=?"
```

`knowledge.py` 的 `_sync_baseline_files()` 查重条件：

```python
# 当前（有 bug）：
"WHERE repo_key=*** AND scope='baseline' AND file_path=? ORDER BY id DESC LIMIT 1"

# 改为：
"WHERE repo_key=*** AND scope='baseline' AND name=? ORDER BY id DESC LIMIT 1"
```

name 是固定中文展示名（"项目结构图谱"/"项目能力清单"/"项目元数据"），不受路径格式变化影响。repo_key 含 provider 前缀，天然区分跨 provider 同名仓库。查询和 UPDATE 都在现有 `db_lock` 内，并发安全。

### 4.3 list_member_docs() 解析逻辑修复

routes/knowledge.py:187 从 file_path 解析 kind/owner 的逻辑改为从 `project-info/` 位置后解析：

```python
# 当前（有 bug）：
parts = file_path.replace("\\", "/").split("/")
if len(parts) >= 4 and parts[0] == "project-info":
    kind = parts[1]
    owner = parts[2]

# 改为：
fp_norm = file_path.replace("\\", "/")
if "project-info/" in fp_norm:
    rel = fp_norm.split("project-info/", 1)[1]  # member/user/file.md 或 agent/coding/file.md
    rel_parts = rel.split("/")
    if len(rel_parts) >= 2:
        kind = rel_parts[0]  # "member" 或 "agent"
        owner = rel_parts[1]
    else:
        kind = "member"
        owner = created_by or ""
else:
    kind = "member"
    owner = created_by or ""
```

兼容所有三种 file_path 格式（OLD/PARTIAL/NEW）。

### 4.4 删除废弃接口

删除以下废弃接口和函数，减少 file_path 依赖面：

- `GET /knowledge/content`（routes/knowledge.py:394 `read_knowledge_content`）
- `GET /knowledge/context`（routes/knowledge.py:428 `knowledge_context`）
- `DELETE /knowledge` query param 方式（routes/knowledge.py:459 `delete_knowledge`）
- `_read_file()`（routes/knowledge.py:72）
- `_read_ws_file()`（routes/knowledge.py:110）
- `_safe_rel()` 和 `ALLOWED_EXTS`（如果只被上述废弃函数使用）

注意：`DELETE /knowledge/{knowledge_id}`（routes/knowledge.py:875）保留，这是前端实际使用的按 id 删除接口。`_update_knowledge_content`（routes/knowledge.py:641）保留，被 `PUT /knowledge/{id}` 调用。

删除后 knowledge 路由所有接口全部以 id 或 repo_key 为查询条件，没有任何接口再依赖 file_path 做 WHERE 查询。

### 4.5 read_entry_content() 本地兜底兼容

```python
if file_path:
    full = os.path.normpath(os.path.join(WORKSPACE_DIR, file_path))
    if not os.path.isfile(full):
        # 兼容旧格式 file_path（不含 provider 前缀）
        provider, _, _ = parse_repo_key(repo_key)
        alt = os.path.join(WORKSPACE_DIR, provider or 'gitcode', file_path)
        if os.path.isfile(alt):
            full = alt
```

迁移后旧格式 file_path 记录会被 UPDATE 为新格式，此兼容逻辑只做过渡期兜底。

### 4.6 数据清理迁移函数

#### 异步执行方式

迁移函数放在 `src/api/main.py` 的 `lifespan()` 函数内，用 daemon thread 异步执行，不阻塞主流程应用启动。与项目中已有的 `_knowledge_cleanup_loop`（main.py:168）同模式。

迁移设计为最终一致性：迁移期间用户可能读到旧格式数据（旧 obs_key/file_path 仍可用），迁移完成后自动切换到新格式。不要求迁移完成才能接流量。

调用位置：`src/api/main.py` 的 `lifespan()` 函数内，后台任务启动区：

```python
# main.py lifespan() 内，与 _knowledge_cleanup_loop 同区域
try:
    from src.api.db_setup import _migrate_issue190
    import threading
    _t190 = threading.Thread(target=_migrate_issue190, daemon=True)
    _t190.start()
    log.info("Issue #190 migration started (async)")
except Exception as e:
    log.warning(f"Issue #190 migration start failed: {e}")
```

#### 幂等控制机制

通过 meta 表 `issue190_migrate_done` 标记控制，与 Issue #67 的 `issue95_migrate_done` 同模式：

```python
def _migrate_issue190():
    from src.session_manager import db, db_lock
    with db_lock:
        row = db.cursor().execute(
            "SELECT value FROM meta WHERE key='issue190_migrate_done'"
        ).fetchone()
        if row and row[0] == '1':
            return  # 已执行，跳过

    # 执行迁移（步骤 1~3）
    ...

    # 标记完成（全部步骤成功后才写标记）
    with db_lock:
        db.cursor().execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('issue190_migrate_done', '1')"
        )
        db.commit()
```

#### 失败重试的幂等保障

如果迁移中途失败，标记未写，下次启动会重新执行。每个步骤自身幂等：

| 步骤 | 重复执行时的行为 | 幂等依据 |
|------|----------------|---------|
| baseline 去重 DELETE | 第二次没有重复行可删，DELETE 0 行 | 按 repo_key+name 分组保留 MAX(id)，已删除的 id 不在表中 |
| obs_key UPDATE | 旧 obs_key 已被改为新格式，`旧 == 新` 跳过 | 每条记录计算新 key 后先比较，相同则 skip |
| obs_key UPDATE + OBS 写入 | OBS 同 key 同内容写入，结果不变 | OBS save_bytes 幂等（覆盖写） |
| file_path UPDATE | 已是新格式的记录跳过 | 前缀判断跳过已归一化的记录 |

标记只在步骤 1~3 全部成功后才写。部分失败时已成功的记录会跳过，只处理剩余记录。

#### 步骤 1：清理 baseline 重复记录

按 `repo_key + name` 分组，保留 id 最大的一条，删除其余。先删 scene_knowledge_ref 中的关联引用，再删 agent_shared_knowledge 中的重复行。

```sql
-- SQLite
DELETE FROM scene_knowledge_ref
WHERE knowledge_id IN (
    SELECT id FROM agent_shared_knowledge
    WHERE scope='baseline'
    AND id NOT IN (
        SELECT MAX(id) FROM agent_shared_knowledge
        WHERE scope='baseline'
        GROUP BY repo_key, name
    )
);

DELETE FROM agent_shared_knowledge
WHERE scope='baseline'
AND id NOT IN (
    SELECT MAX(id) FROM agent_shared_knowledge
    WHERE scope='baseline'
    GROUP BY repo_key, name
);
```

MySQL 版本用临时表（与 Issue #67 同模式）。

#### 步骤 2：更新所有记录的 obs_key 为新格式（含 provider）

obs_key 迁移不能简单补前缀，需按 scope 分别处理：

**baseline / project / issue / issue_agent**（OLD_A 和 OLD_B 格式）：

```python
provider, owner, repo = parse_repo_key(repo_key)
new_prefix = f"knowledge/{provider}/{owner}/{repo}"

if obs_key.startswith(new_prefix):
    skip  # 已是新格式
elif obs_key.startswith("project-info/"):
    # OLD_A 格式：project-info/gitcode:{owner}/{repo}/{fname}
    # 完全重写
    fname = os.path.basename(obs_key)
    if scope == "baseline":
        new_obs_key = f"{new_prefix}/project-info/{fname}"
    else:
        # project/issue/issue_agent 从 file_path 推导相对路径
        new_obs_key = knowledge_storage_key(repo_key, scope, agent_type, issue_num)
elif obs_key.startswith(f"knowledge/{owner}/{repo}"):
    # OLD_B 格式：knowledge/{owner}/{repo}/...
    # 在 knowledge/ 后插入 provider/
    new_obs_key = f"knowledge/{provider}/{obs_key[len('knowledge/'):]}"
```

**member**（可能含路径重复）：

```python
# 从旧 obs_key 中提取 member/ 或 agent/ 后的相对路径
for marker in ("member/", "agent/"):
    idx = obs_key.find(marker)
    if idx >= 0:
        rel = obs_key[idx:]
        break
else:
    rel = os.path.basename(obs_key)
new_obs_key = f"{new_prefix}/{rel}"
```

这样 member 的 obs_key 统一为 `knowledge/{provider}/{owner}/{repo}/member/{username}/{filename}`，同时修复路径重复问题。

OBS 对象迁移：旧 key != 新 key 时，从 OBS 旧 key 读取内容 → 写入 OBS 新 key → UPDATE DB obs_key。不删除旧 OBS 对象。旧 key 读不到内容时只 UPDATE DB obs_key。

并发安全：OBS 操作是幂等的（覆盖写）。DB UPDATE 在 `db_lock` 内。迁移期间并发读取可能走 `_knowledge_cache`——缓存 key 是 obs_key，迁移更新 obs_key 后旧缓存 key 自然 miss。

#### 步骤 3：更新所有记录的 file_path 为新格式

file_path 迁移需判断当前格式，不能简单补前缀（PARTIAL 格式已有 owner/repo，只缺 provider）：

```python
provider, owner, repo = parse_repo_key(repo_key)
full_prefix = f"{provider}/{owner}/{repo}/"
partial_prefix = f"{owner}/{repo}/"

if file_path.startswith(full_prefix):
    skip  # 已是 NEW 格式
elif file_path.startswith(partial_prefix):
    # PARTIAL 格式：已有 owner/repo，只补 provider
    new_file_path = f"{provider}/{file_path}"
elif file_path.startswith("project-info/"):
    # OLD 格式：只有 project-info/，补完整前缀
    new_file_path = f"{full_prefix}{file_path}"
else:
    skip  # 未知格式，不处理
```

并发安全：纯 DB UPDATE，在 `db_lock` 内。file_path 只用于本地文件读取兜底和 member 列表/删除查询。并发写入新记录时 file_path 已是新格式，不会冲突。

## 5. 改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| `src/repo_key.py` | 新增函数 | `knowledge_obs_prefix(repo_key)` |
| `src/project_info/builder.py` | 修改 | `_write_to_obs()` 查重改 name + obs_key 用新前缀 |
| `src/api/knowledge.py` | 修改 | `knowledge_storage_key()` 用新前缀；`_sync_baseline_files()` 查重改 name + obs_key 用新前缀；sync 循环 member obs_key 改为从 file_path 提取 member/agent 相对路径（修复路径重复）；`read_entry_content()` 本地兜底兼容旧格式 file_path |
| `src/api/routes/knowledge.py` | 修改+删除 | member 上传 obs_key 用新前缀；`list_member_docs()` 解析逻辑修复；删除废弃接口 `GET /content`、`DELETE /` query param、`_read_file()`、`_read_ws_file()` |
| `src/api/db_setup.py` | 新增迁移函数 | `_migrate_issue190()`：baseline 去重 + obs_key 归一化（按 scope 分别处理）+ file_path 归一化（按格式分别处理） |
| `src/api/main.py` | 新增调用 | lifespan() 内启动 `_migrate_issue190` daemon thread |

## 6. 不受影响

- `load_project_info()` / `load_project_info_with_obs()`：按 `os.path.basename(fp)` 做 dict key，重复记录会覆盖（last wins），obs_key 变更后读取路径自动更新。
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
- member 列表：file_path 归一化后正确解析 kind/owner（三种格式均覆盖）
- member 删除：file_path 归一化后 WHERE 条件能匹配到记录
- member obs_key：upload 和 sync 路径生成的格式一致，无路径重复
- 迁移幂等：多次启动不重复执行迁移
- 迁移期间并发 webhook：不产生新重复记录
- 迁移期间并发读取：read_entry_content 旧格式兼容生效

## 8. 确认点

1. baseline 去重维度用 repo_key + name（而非 file_path）——确认？
2. obs_key 新格式：`knowledge/{provider}/{owner}/{repo}/...`——确认？
3. member obs_key 统一格式：`knowledge/{provider}/{owner}/{repo}/member/{username}/{filename}`——确认？
4. member sync 路径 obs_key 生成从 file_path 提取 member/agent 相对路径（不用 replace）——确认？
5. 数据清理保留 id 最大的一条（新格式记录）——确认？
6. OBS 旧对象不删除，只更新 DB obs_key——确认？
7. 迁移函数用 daemon thread 异步执行（放在 main.py lifespan() 内），最终一致性设计，通过 meta_value 幂等控制——确认？
8. `read_entry_content()` 本地兜底加旧格式 file_path 兼容——确认？
9. `list_member_docs()` 解析逻辑从 `project-info/` 位置后解析 kind/owner——确认？
10. file_path 迁移按格式分别处理（OLD 补完整前缀、PARTIAL 只补 provider、NEW 跳过）——确认？
11. obs_key 迁移按 scope 分别处理（baseline/project/issue/issue_agent insert provider，member 提取 member/agent 相对路径重新拼接）——确认？
