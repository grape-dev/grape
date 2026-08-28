# 技能交付断链修复 — skill_id 直传方案

状态: 设计待评审
分支: 待定（建议 feat/skill-id-delivery）
前置: 排查确认 blog-skill-creator / blog-skill-tester 等 user/repo scope 技能无法交付到 worker

## 1. 问题

### 1.1 现象

仓库 `xiaowu2119/my-skill`（dev_scene=skill-dev）的 coding agent 绑定了 `blog-skill-creator`（skill_id=2643, scope=user, owner_id=2）。

实际任务 `t_72184cc0e372` 的 skill_files = `{"blog-skill-creator": {}}` —— 空字典，worker 没收到任何 SKILL.md 内容。blog-skill-tester 同样空。

对比 official scope 技能（implementation、issue-advance、vod 等）均正常交付。

### 1.2 根因

session 里有 skill_id=2643，event_pipeline 也正确读到了。但 enqueue_task 不接受 skill_id 参数，内部重新调 `resolve_skill_id(skill_name, owner_id=account)` 按名字解析——account 传的是仓库名 "xiaowu2119/my-skill"，skill 表里 user scope 的 owner_id 存的是用户ID "2"，对不上 → 返回 0 → 技能打包跳过。

claim 路径的 `_attach_skill_obs` 同样调 `resolve_skill_id(skill)` 不传 owner_id，只查 scope='official' → 查不到 → 不附 OBS URL。

### 1.3 影响范围

所有只存在于 user/repo scope、没有 official 同名别名的技能都受影响（共 11 个技能名）：

```
blog-skill-creator, blog-skill-tester, build, chat-im, implement,
issue-advance-fix, repo-to-k8s-iac, sre-cloud-pro, vod-pro,
wecomcli-contact, alibabacloud-agentbay-aio-skills
```

## 2. 方案

核心思路：session 里已有 skill_id，直接传到 enqueue_task 和 claim 路径，用 `get_skill_by_id(skill_id)` 查 skill 表拿 skill_path 和 file_path，跳过 resolve_skill_id 的 owner_id 匹配。

### 2.1 方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| **A. skill_id 直传（本方案）** | 唯一标识精确匹配，不依赖 owner_id 映射；official/user/repo 三种 scope 统一有效；向后兼容（skill_id=0 走老逻辑） | 需加 DB 列 + 改 10 个点 |
| B. 修 resolve_skill_id owner_id 映射 | 改动面小 | user scope owner_id=user_id、repo scope owner_id=repo_key，两种格式分叉；多一次 DB 查询做 username→user_id 映射；没解决"丢弃已有 skill_id 重新解析"的根本问题 |
| C. resolve_skill_id 无 owner_id 兜底 | 最简单 | 不精确，同名 skill 可能匹配到别人的；安全隐患 |

## 3. 改动清单（10 个点，8 个文件）

### 3.1 DB 层：agent_tasks 加 skill_id 列

**文件**: `src/api/db_setup.py`

agent_tasks 表加列 `skill_id INT DEFAULT 0`。用现有 `_ensure_column` 幂等迁移，放在 db_setup.py 第 1446 行的 agent_tasks 补列块中（issue_session_id / project_info_files 旁边），同样的 try/except + db.commit 模式。init_database() 启动时自动执行，无需手动 DDL。

```python
_ensure_column(c, "agent_tasks", "skill_id", "INTEGER DEFAULT 0")
```

`_ensure_column` 内部用 PRAGMA table_info 检查列是否存在，MySQLCompatDB 将 PRAGMA 翻译为 information_schema 查询，MySQL 和 SQLite 均兼容。

### 3.2 service.py：_TASK_COLUMNS + 占位符

**文件**: `src/runtime/service.py`

`_TASK_COLUMNS` 字符串加 `skill_id`（插在 skill 后面）：

```python
_TASK_COLUMNS = (
    "id, agent_session_id, runtime_id, event_seq, issue_id, repo_key, agent_type, skill, "
    "skill_id, "  # ← 新增
    "prompt, cwd, runtime_session_id, event_type, account, user_id, repo_gitcode_token, "
    "env_extra, skill_files, server_url, issue_session_id, project_info_files, project_info_meta, "
    "knowledge_files, attempts, pool_agent_type, affinity_runtime_id, "
    "status, exit_code, result_text, errors, stderr, "
    "usage_json, started_at, completed_at, created_at"
)
```

两处 INSERT 占位符从硬编码 34 改为动态计算（service.py:339 和 event_pipeline.py:65）：

```python
f"({','.join(['?'] * len(_TASK_COLUMNS_LIST))})"
```

`get_task` / `_row_to_dict` 自动包含 skill_id（zip 映射），无需额外改。

### 3.3 service.py：enqueue_task 签名 + skill 打包逻辑

**文件**: `src/runtime/service.py`

签名加参数 `skill_id: int = 0`。

skill 打包逻辑改为 skill_id 优先，包含三段降级链：

1. skill_id > 0 且 `get_skill_by_id` 返回非空 → 直接用主键查，拿 `get_skill_path(skill_id)` 绝对路径 + file_path（OBS 对象键）
2. skill_id > 0 但 `get_skill_by_id` 返回 None（skill 被删除/停用）→ 回退到 `resolve_skill_id` 按名字解析
3. skill_id == 0 → 走老逻辑（向后兼容）

```python
def enqueue_task(*, agent_session_id, runtime_id, prompt, cwd="",
                 ...,
                 skill="", skill_path="", skill_id=0,  # ← 新增
                 ...):
    ...
    skill_files = {}
    if skill:
        try:
            from src.user.db import resolve_skill_id, get_skill_path, get_skill_object_key, get_skill_by_id

            if skill_id:
                _sk = get_skill_by_id(skill_id)
                if _sk:
                    # ① skill_id 有效：直接按主键查
                    _sid = skill_id
                    _path = get_skill_path(skill_id)  # 绝对路径（三 scope 统一）
                    _okey = _sk.get("file_path") or ""
                else:
                    # ② skill_id 失效（skill 被删除/停用）→ 回退按名字解析
                    _sid = resolve_skill_id(skill, owner_id=account) or resolve_skill_id(skill)
                    _path = get_skill_path(_sid) if _sid else (skill_path or "")
                    _okey = get_skill_object_key(_sid) if _sid else ""
            else:
                # ③ 无 skill_id → 走老逻辑（向后兼容）
                _sid = resolve_skill_id(skill, owner_id=account) or resolve_skill_id(skill)
                _path = get_skill_path(_sid) if _sid else (skill_path or "")
                _okey = get_skill_object_key(_sid) if _sid else ""

            # 本地目录存在 → 打包（official 技能 + 少数有磁盘目录的 user/repo 技能）
            if _path and os.path.isdir(os.path.dirname(_path)):
                skill_files = _pack_skill_dir(skill, _path)
            # file_path 非空 → OBS zip 提取（user/repo 技能主要走此路径）
            if _okey and not skill_files:
                from src.storage import get_skill_zip_bytes
                _zip = get_skill_zip_bytes(_okey)
                if _zip:
                    skill_files = _skill_files_from_zip(_zip, skill)
        except Exception as e:
            log.warning(f"enqueue: skill pack failed for {skill}: {e}")
```

INSERT 语句加 skill_id 值（在 skill 后面）：

```python
(task_id, agent_session_id, runtime_id, event_seq, issue_id or "", repo_key or "",
 agent_type or "", skill or "", skill_id or 0,  # ← 新增
 prompt, cwd or "", runtime_session_id or "", ...)
```

**关键点**：必须用 `get_skill_path(skill_id)` 拿绝对路径，不能用 `_sk.get("skill_path")`。skill 表里的 skill_path 字段存的是相对值（如 "implementation"）或空串（user/repo scope），`_pack_skill_dir` 的 `dirname()` 对相对值返回空串 → 打包失败。`get_skill_path` 按 scope 拼完整路径：official→`SKILLS_DIR/{path}/SKILL.md`，user→`SKILLS_DIR/custom/{owner_id}/{path}/SKILL.md`，repo→`SKILLS_DIR/repo/{owner}/{path}/SKILL.md`。

**stale skill_id 回退**：MySQL 中 219 个 session 有 skill_id > 0，其中 43 个指向已删除/停用的 skill 记录。这些 session 的 skill_name 都是 official 技能名，`resolve_skill_id(skill_name)` 能找到对应的 active official 记录。回退后行为与当前一致，不会回归。

### 3.4 runtime.py：_attach_skill_obs

**文件**: `src/api/routes/runtime.py`

claim 返回的 task dict 已包含 skill_id 列。改为 skill_id 优先 + stale 回退：

```python
def _attach_skill_obs(task: dict) -> None:
    try:
        from src.storage import get_storage
        _storage = get_storage()
        if _storage is None:
            return
        skill = (task.get("skill") or "").strip()
        if not skill:
            return
        from src.user.db import resolve_skill_id, get_skill_object_key, get_skill_by_id

        _skill_id = task.get("skill_id") or 0
        if _skill_id:
            _sk = get_skill_by_id(_skill_id)
            if _sk:
                _okey = _sk.get("file_path") or ""
            else:
                # stale skill_id → 回退
                _sid = resolve_skill_id(skill)
                _okey = get_skill_object_key(_sid) if _sid else ""
        else:
            _sid = resolve_skill_id(skill)
            _okey = get_skill_object_key(_sid) if _sid else ""

        if not _okey:
            return
        ttl = int(os.environ.get("RUNTIME_SKILL_URL_TTL", "1800"))
        task["skill_key"] = _okey
        task["skill_obs_url"] = _storage.temporary_url(_okey, expires=ttl)
        task["mode"] = "obs"
    except Exception as e:
        log.warning(f"attach skill_obs failed: {e}")
```

### 3.5 runtime.py：runtime_task_skill_zip（服务端代拉端点）

**文件**: `src/api/routes/runtime.py`

同样改为 skill_id 优先 + stale 回退（逻辑与 3.4 相同）：

```python
_skill_id = task.get("skill_id") or 0
if _skill_id:
    _sk = get_skill_by_id(_skill_id)
    if _sk:
        _okey = _sk.get("file_path") or ""
    else:
        _sid = resolve_skill_id(skill)
        _okey = get_skill_object_key(_sid) if _sid else ""
else:
    _sid = resolve_skill_id(skill)
    _okey = get_skill_object_key(_sid) if _sid else ""
```

### 3.6 event_pipeline.py：传 skill_id 到 enqueue_task（2 处）

**文件**: `src/api/event_pipeline.py`

#### 3.6.1 _enqueue_pipeline_task（line 934）

已有 skill_id 变量（从 session 读取，参数传入），加到 enqueue_task 调用：

```python
tid = enqueue_task(
    ...
    skill=skill_name,
    skill_path=skill_path,
    skill_id=skill_id,  # ← 新增
    ...
)
```

#### 3.6.2 _dispatch_subscription_agent（line 415）

同样有 skill_id 变量，走自己的 enqueue 路径，也加 `skill_id=skill_id`。

### 3.7 event_pipeline.py：direct 执行路径（3 处）

**文件**: `src/api/event_pipeline.py`

#### 3.7.1 _create_direct_task（line 32）

函数签名加 `skill_id` 参数，INSERT 加 skill_id 值（在 skill 后面），占位符改 `len(_TASK_COLUMNS_LIST)`。

run_pipeline 调用处传入 skill_id：

```python
_create_direct_task(repo_key, seq, agent_type, _be, skill_name,
                    skill_path, engine_id, evt, issue_id, skill_id=skill_id)
```

#### 3.7.2 _execute_agent_inner（line 1048）

direct 模式不走 enqueue_task，不做 skill_files 打包，靠 `{{SKILL_DIR}}` 占位符替换。当前 user/repo scope 技能的 skill_path 是相对路径，dirname 后为空，替换不了；OBS 技能磁盘上也没有文件。

改为：skill_id > 0 时用 `get_skill_path(skill_id)` 拿绝对路径；file_path 非空（OBS 技能）时从 OBS 拉 zip 解压到 cwd/.skills/ 后替换占位符。

run_pipeline 在 direct 分支前把 skill_id 放进 evt：

```python
evt["_skill_id"] = skill_id
```

_execute_agent_inner 里的替换逻辑：

```python
try:
    from src.user.db import get_skill_by_id, get_skill_path
    _skill_id = evt.get("_skill_id") or 0
    if _skill_id:
        _sk = get_skill_by_id(_skill_id)
        if _sk:
            _okey = _sk.get("file_path") or ""
            if _okey:
                # OBS 技能：解压到 cwd/.skills/
                # zip 根 = {skill_name}/，落地 .skills/{skill_name}/SKILL.md
                # 注意：解压到 .skills/ 而非 .skills/{skill_name}/，否则双层嵌套
                from src.storage import get_skill_zip_bytes
                _zip = get_skill_zip_bytes(_okey)
                if _zip:
                    import zipfile, io
                    _skill_base = os.path.join(ws, ".skills")
                    os.makedirs(_skill_base, exist_ok=True)
                    with zipfile.ZipFile(io.BytesIO(_zip)) as zf:
                        zf.extractall(_skill_base)
                    prompt = prompt.replace("{{SKILL_DIR}}", _skill_base)
                    env_extra["DEVOPS_SKILLS_DIR"] = _skill_base
            else:
                # 本地技能：用绝对路径
                # get_skill_path 返回 SKILLS_DIR/implementation/SKILL.md
                # dirname(dirname(...)) = SKILLS_DIR
                # prompt 中 {{SKILL_DIR}}/implementation/SKILL.md → SKILLS_DIR/implementation/SKILL.md
                _full = get_skill_path(_skill_id)
                if _full and os.path.exists(_full):
                    prompt = prompt.replace("{{SKILL_DIR}}", os.path.dirname(os.path.dirname(_full)))
    else:
        # 向后兼容：无 skill_id 时走老逻辑
        _real_skill_dir = os.path.dirname(evt.get("_skill_path", "")) if evt.get("_skill_path") else ""
        if _real_skill_dir:
            prompt = prompt.replace("{{SKILL_DIR}}", _real_skill_dir)
except Exception:
    pass
```

**路径层级说明**：
- prompt_builder 生成的占位符是 `{{SKILL_DIR}}/{skill_name}/SKILL.md`
- OBS zip 解压到 `.skills/`，zip 内结构 `{skill_name}/SKILL.md` → 落地 `.skills/{skill_name}/SKILL.md`
- `{{SKILL_DIR}}` 替换为 `.skills/` → `.skills/{skill_name}/SKILL.md` ✓
- official 技能 `get_skill_path` 返回 `SKILLS_DIR/implementation/SKILL.md`，`dirname(dirname(...))` = `SKILLS_DIR` → `SKILLS_DIR/implementation/SKILL.md` ✓

### 3.8 chat.py：传 skill_id

**文件**: `src/api/routes/chat.py`

chat.py 的 `_get_chat_config` 已返回 skill_id，`_enqueue_chat` 函数签名已有 skill_id 参数，但 enqueue_task 调用没传。两处调用（line 396 和 line 588）都加：

```python
tid = enqueue_task(
    ...
    skill=skill_name or "chat",
    skill_id=skill_id,  # ← 新增
    ...
)
```

### 3.9 vod_handler_async.py + onboarding.py：传 skill_id

**文件**: `src/im/vod_handler_async.py`、`src/api/onboarding.py`

vod_handler_async.py 已从 session 读 skill_id（line 269-272）但未传给 enqueue_task。onboarding.py 的 `_load_vod_session` 读 skill_id 但未返回。

```python
# vod_handler_async.py:305
tid = enqueue_task(
    ...
    skill=skill, skill_path=skill_path or "",
    skill_id=agent_session.get("skill_id", 0) or 0,  # ← 新增
)

# onboarding.py: _load_vod_session 需额外返回 skill_id
# onboarding.py:196
tid = enqueue_task(
    ...
    skill=skill,
    skill_path=os.path.join(SKILLS_DIR, skill, "SKILL.md"),
    skill_id=skill_id,  # ← 新增
)
```

### 3.10 events.py：重派传 skill_id

**文件**: `src/api/routes/events.py`

重派从 `get_task(task_id)` 读旧 task dict，旧 task 在 DB 里有 skill_id 列。必须传，否则 user/repo 技能重派仍然断链：

```python
enqueue_task(
    ...
    skill=old.get("skill") or "",
    skill_id=old.get("skill_id") or 0,  # ← 必须传
    ...
)
```

## 4. 不需要改的调用点（2 个）

| 调用点 | 技能 | scope | skill_id | 原因 |
|--------|------|-------|----------|------|
| events.py:509 | event-dialogue | official | 0 | official 技能，走老逻辑正常 |
| handler_async.py:764 | chat（硬编码） | official | 0 | official 技能，走老逻辑正常 |

## 5. 不需要改的组件

| 组件 | 原因 |
|------|------|
| worker（src/runtime/agent.py） | 只用 skill_obs_url 和 skill_files，不直接用 skill_id。两个数据源都被本方案修正 |
| prompt_builder.py | 不用 skill_id，按 skill_name 生成 `{{SKILL_DIR}}/{skill_name}/SKILL.md` |
| executor.py | direct 模式下用 SKILLS_DIR 替换 {{SKILL_DIR}}（official 技能兜底），不需要 skill_id |
| 前端（app.js / pages/*.js） | 只从 session 配置读 skill_id（agents.js、chat.js），不从 task 记录读 |
| 测试（tests/） | conftest.py 调 init_database() 自动建表，_TASK_COLUMNS 改动自动生效；test_claim_response_lost.py 调 enqueue_task 不传 skill_id → 默认 0 → 走老逻辑 |
| runtime.py 列表查询（line 405/436） | 用自己的列列表（不通过 _TASK_COLUMNS），不包含 skill_id，无影响 |

## 6. 改动文件总览

| # | 文件 | 改动点 | 行数估计 |
|---|------|--------|----------|
| 1 | src/api/db_setup.py | agent_tasks 加 skill_id 列（幂等迁移） | +3 |
| 2 | src/runtime/service.py | _TASK_COLUMNS 加 skill_id + 占位符改 len() + enqueue_task 签名 + skill 打包逻辑(含 stale 回退) + INSERT | +35 |
| 3 | src/api/routes/runtime.py | _attach_skill_obs(含 stale 回退) + runtime_task_skill_zip(含 stale 回退) | +25 |
| 4 | src/api/event_pipeline.py | _enqueue_pipeline_task + _dispatch_subscription_agent + _create_direct_task(签名+INSERT+占位符) + _execute_agent_inner + run_pipeline | +35 |
| 5 | src/api/routes/chat.py | _enqueue_chat + chat stream enqueue 传 skill_id | +4 |
| 6 | src/im/vod_handler_async.py | enqueue_task 传 skill_id | +2 |
| 7 | src/api/onboarding.py | _load_vod_session 返回 skill_id + enqueue_task 传参 | +6 |
| 8 | src/api/routes/events.py | 重派 enqueue_task 传 skill_id | +2 |
| | **合计** | 10 个改动点，8 个文件 | **~112 行** |

## 7. 向后兼容性

- skill_id 参数默认 0，不传时走老逻辑（resolve_skill_id），不影响现有调用方
- agent_tasks.skill_id 默认 0，老任务无此值，claim 时 skill_id=0 走老逻辑
- DB 迁移幂等，init_database() 自动执行，无需手动 DDL
- official scope 技能 skill_id 可传可不传，两种路径都能正确解析
- stale skill_id（指向已删除 skill）自动回退到 resolve_skill_id，行为与当前一致
- 测试不受影响：conftest.py 调 init_database() 自动建表，_TASK_COLUMNS 改动自动生效

## 8. 技能交付降级链（完整）

任务从创建到 worker 执行，技能交付有三条降级路径：

```
enqueue_task 打包 skill_files（服务端）
    │
    ├── skill_id > 0, get_skill_by_id 有效
    │   ├── file_path 非空 → OBS zip 提取文本 → skill_files 非空
    │   └── file_path 空, 本地目录存在 → _pack_skill_dir → skill_files 非空
    │
    ├── skill_id > 0, get_skill_by_id None（stale）
    │   └── 回退 resolve_skill_id → 找到 official skill → _pack_skill_dir
    │
    └── skill_id = 0
        └── resolve_skill_id(skill_name, owner_id) → 老逻辑

claim 时 _attach_skill_obs 签 OBS URL（服务端）
    │
    ├── skill_id > 0, get_skill_by_id 有效 → file_path → 签预签名 URL
    ├── skill_id > 0, stale → 回退 resolve_skill_id → file_path → 签 URL
    └── skill_id = 0 → resolve_skill_id → file_path → 签 URL

worker 收到任务后（worker 端）
    │
    ├── skill_obs_url 非空 → 直连 OBS 拉 zip → 解压到 .skills/
    │   └── 失败 → POST /tasks/{id}/skill-zip 服务端代拉 → 解压
    │
    └── skill_obs_url 空 → 用 skill_files 文本物化到 .skills/
```

direct 模式（本机执行）不走 claim，在 _execute_agent_inner 里直接处理：

```
_execute_agent_inner
    │
    ├── skill_id > 0, get_skill_by_id 有效
    │   ├── file_path 非空 → OBS zip 解压到 .skills/ → 替换 {{SKILL_DIR}}
    │   └── file_path 空 → get_skill_path → 替换 {{SKILL_DIR}} 为 SKILLS_DIR
    │
    └── skill_id = 0 / stale → 老逻辑 / executor.py 用 SKILLS_DIR 兜底
```

## 9. 验证方案

### 9.1 单元验证

1. `get_skill_by_id(2643)` 返回 file_path 非空 → `_skill_files_from_zip` 提取 6 个文件全成功
2. enqueue_task 传 skill_id=2643 → skill_files 非空（包含 SKILL.md + references + templates + scripts）
3. enqueue_task 传 skill_id=0 + skill="implementation" → 走老逻辑 → skill_files 非空（向后兼容）
4. enqueue_task 传 skill_id=1106（已删除）+ skill="implementation" → stale 回退 → resolve_skill_id 找到 official → skill_files 非空
5. claim 返回的 task 包含 skill_id → _attach_skill_obs 附上 skill_obs_url
6. direct 模式 skill_id=2643 → .skills/blog-skill-creator/ 解压成功 → {{SKILL_DIR}} 替换为 .skills/
7. direct 模式 skill_id=2588（official implementation）→ {{SKILL_DIR}} 替换为 SKILLS_DIR

### 9.2 端到端验证

1. 向 `xiaowu2119/my-skill` Issue #3 重发 A2A coding 任务
2. 检查 agent_tasks.skill_files 非空（包含 SKILL.md + references + scripts）
3. 检查 worker 端 .skills/blog-skill-creator/ 目录存在且文件完整
4. 检查 agent 日志中有技能引用（{{SKILL_DIR}} 被替换）

### 9.3 回归验证

1. official scope 技能（implementation、issue-advance）任务正常执行
2. 无 skill_id 的老调用方（events.py:509 event-dialogue、handler_async.py:764 chat）正常执行
3. stale skill_id session（43 个，全是 official 技能名）任务正常执行
4. 重派场景（events.py:239）正常执行，skill_id 正确继承
5. chat 配置了自定义技能（chat-im, scope=repo）的会话正常执行

## 10. 风险

| 风险 | 概率 | 缓解 |
|------|------|------|
| _TASK_COLUMNS 改动导致 INSERT 字段数不匹配 | 中 | 占位符改 len(_TASK_COLUMNS_LIST) 动态计算 + 启动时冒烟 |
| direct 模式 OBS zip 解压失败（网络/OBS 不可达） | 低 | try/except 兜底，失败时 {{SKILL_DIR}} 保留，executor.py 用 SKILLS_DIR 兜底（official 技能） |
| MySQL ALTER TABLE 加列锁表 | 低 | INT DEFAULT 0，MySQL 8.0 秒级加列 |
| stale skill_id 回退到 resolve_skill_id 仍失败（skill_name 在 DB 中不存在） | 极低 | 当前 43 个 stale session 全是 official 技能名，resolve_skill_id 必命中；未来可加定时清理 stale session |
| _skill_files_from_zip 跳过二进制文件 | 已知 | OBS zip 路径（skill_obs_url）用 extractall 全量解压，包含二进制；skill_files 文本路径仅作降级 |
