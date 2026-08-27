# 知识库 OBS 存储改造方案

## 背景

当前知识库（agent_shared_knowledge）正文存在工作区本地文件，多实例集群部署后本地文件不可共享。需要改造为 OBS 存储，与 skill 表的 OBS 改造同模式。

## 一、DB 变更

agent_shared_knowledge 表新增 2 个字段：

```
obs_key VARCHAR(512) DEFAULT ''
content_hash VARCHAR(64) DEFAULT ''
```

迁移语句（加到 db_setup.py migrations 列表）：
```sql
ALTER TABLE agent_shared_knowledge ADD COLUMN obs_key VARCHAR(512) DEFAULT ''
ALTER TABLE agent_shared_knowledge ADD COLUMN content_hash VARCHAR(64) DEFAULT ''
```

DDL 同步修改（新库建表时包含 obs_key + content_hash）。

向前兼容逻辑：
- obs_key 非空 → 走 OBS（storage.read_bytes(obs_key)）
- obs_key 为空 → 走本地（open(file_path)，现有数据不破坏）
- storage 为 None（OBS 未启用）→ 全部回退本地

content_hash 作用：
- 写入时计算 hash 存 DB（hashlib.sha256(content).hexdigest()[:64]）
- 读取时查 DB hash 对比本地缓存 hash → 一样用缓存 / 不一样从 OBS 重新拉
- 保证集群多实例缓存一致性——DB hash 变了所有实例立即发现

与 skill 表完全同模式：skill 用 skill_path（本地）+ file_path（OBS），knowledge 用 file_path（本地）+ obs_key（OBS）+ content_hash（缓存一致性）。

## 二、OBS 对象键设计

命名规则（参考 skill_key 的前缀模式）：

```
knowledge/{repo_key}/{scope}/[issue/{n}/][{agent}/]knowledge.md
knowledge/{repo_key}/member/{username}/{filename}
knowledge/{repo_key}/agent/{agent_type}/{filename}
```

示例：
```
knowledge/gitcode:songspace/shelldev/project/knowledge.md
knowledge/gitcode:XOcto/BLOG-SYS-5/issue/3/knowledge.md
knowledge/gitcode:XOcto/BLOG-SYS-5/issue_agent/3/sre/knowledge.md
knowledge/gitcode:XOcto/BLOG-SYS-5/member/XOcto/repo-credentials.json
knowledge/gitcode:XOcto/BLOG-SYS-5/agent/sre/blog-config.json
```

在 src/api/knowledge.py 新增 `knowledge_obs_key()` 函数生成键，与 `knowledge_path()` 对称。

## 三、obs_key 生成与写入逻辑（补充）

### write_knowledge

```
OBS 模式（storage 不为 None）：
  1. obs_key = knowledge_obs_key(repo_key, scope, agent_type, issue_num)
  2. 读现有 OBS 内容 → 追加新行 → save_bytes(obs_key, 合并后内容) 覆盖写
  3. DB INSERT 写 obs_key 字段

本地模式（storage 为 None）：
  1. obs_key 留空（''）
  2. 走现有 open(file_path, "a") 追加写
  3. DB INSERT obs_key=''
```

### upload_knowledge

```
OBS 模式：
  1. obs_key = knowledge_obs_key(repo_key, "member", username, filename)
  2. storage.save_bytes(obs_key, file_content)
  3. DB INSERT 写 obs_key

本地模式：
  1. obs_key 留空
  2. 走现有 os.makedirs + open(w) 写本地
  3. DB INSERT obs_key=''
```

### delete_knowledge

```
OBS 模式（obs_key 非空）：
  1. storage.delete_object(obs_key) 删 OBS 对象
  2. DB 行 obs_key 清空（UPDATE obs_key=''）
  3. 如果 DB 行也要删——DELETE 行

本地模式（obs_key 为空）：
  1. 走现有 os.remove 删本地文件
  2. DELETE DB 行
```

**注意：** 删 OBS 对象后必须清空 DB 的 obs_key——否则后续读取走 OBS 但读不到。

## 四、list_keys 元数据问题（补充）

### 问题

现有 `list_keys()` 只返回 key 列表（`list[str]`），不返回 size/lastModified。
但 `_member_docs()` 需要 size 和 updated_at 展示在前端。

### 方案

新增 `list_objects_with_meta()` 方法，返回 `list[dict]`，每个 dict 含 key/size/lastModified：

```python
def list_objects_with_meta(self, prefix: str) -> list[dict]:
    """列举对象并返回元数据（key/size/lastModified）。"""
    ...
    for content in (body.get("contents") or []):
        items.append({
            "key": self._strip_prefix(content.get("key", "")),
            "size": int(content.get("size", 0)),
            "last_modified": content.get("lastModified", ""),
        })
    return items
```

不影响现有 `list_keys()`——skill 模块继续用 list_keys，knowledge 模块用 list_objects_with_meta。

### _member_docs 适配

OBS 模式下：
1. `list_objects_with_meta("knowledge/{repo_key}/member/")` 枚举
2. 每个对象的 size/last_modified 直接取
3. last_modified 格式转换：OBS 返回 "2026/08/23 05:34:43" → 转 "2026-08-23 05:34:43"
4. rel_path 从 obs_key 转换（去掉 knowledge/{repo_key}/ 前缀）

## 五、OBS key 与 rel_path 映射（补充）

### 问题

`member_files_pi()` / `agent_files()` 返回 `{rel_path: content}`。
OBS 模式下 list_keys 返回的是 OBS key 不是 rel_path——需要转换。

### 映射规则

```
OBS key:    knowledge/gitcode:XOcto/BLOG-SYS-5/member/XOcto/repo-credentials.json
            ↓ 去掉 knowledge/{repo_key}/ 前缀
rel_path:   member/XOcto/repo-credentials.json
            ↓ 加 project-info/ 前缀
full rel:   project-info/member/XOcto/repo-credentials.json
```

在 `knowledge_obs_key()` 和读取函数里统一处理映射。

## 六、OBS 读取失败兜底（补充）

### 问题

`load_project_info()` 打包知识到 Worker 时，OBS 读取失败（网络问题）→ 知识文件为空 → Worker 拿不到知识。

### 方案

读取统一逻辑加 try-except 兜底：

```python
if obs_key:
    try:
        content = storage.read_bytes(obs_key)
        if content is None:
            # OBS 读不到——回退本地
            content = open(file_path).read() if file_path else ""
    except Exception:
        # OBS 异常——回退本地
        content = open(file_path).read() if file_path else ""
elif file_path:
    content = open(file_path).read()
else:
    content = ""
```

## 七、改动文件清单

### 1. src/api/db_setup.py
- migrations 加 `ALTER TABLE agent_shared_knowledge ADD COLUMN obs_key VARCHAR(512) DEFAULT ''`
- DDL 加 obs_key 字段

### 2. src/storage/__init__.py + src/storage/obs.py
- 新增 `list_objects_with_meta()` 方法（返回 key/size/lastModified）

### 3. src/api/knowledge.py（核心读写模块）

新增：
- `knowledge_obs_key()` — 按 scope 生成 OBS 对象键

改造 6 个函数：
- `knowledge_path()` — 不改（仍返回本地路径，兼容用）
- `read_knowledge()` — 加 obs_key 判断：非空走 storage.read_bytes(obs_key)，空走本地
- `write_knowledge()` — OBS 模式读-合并-覆盖写 + DB INSERT 写 obs_key；本地模式不变
- `knowledge_files()` — 随 read_knowledge 改造自动生效
- `member_files()` / `member_files_pi()` / `agent_files()` — OBS 模式用 list_objects_with_meta 枚举 + read_bytes 读内容 + key→rel_path 转换；空走 os.walk

### 4. src/api/routes/knowledge.py（API 路由层）

改造 5 个函数：
- `_read_file()` / `_read_ws_file()` — 加 obs_key 判断
- `_member_docs()` — OBS 模式用 list_objects_with_meta，size/lastModified 从返回值取
- `upload_knowledge()` — OBS 模式调 storage.save_bytes，DB INSERT 写 obs_key
- `delete_knowledge()` — OBS 模式调 storage.delete_object + DB obs_key 清空
- `_project_knowledge_entries()` / `_agent_knowledge_entries()` — 查 DB 时多查 obs_key 字段

### 5. src/api/routes/events.py
- `add_knowledge()` — 调 write_knowledge（内部自动生成 obs_key）

### 6. src/runtime/service.py
- `load_project_info()` — 加 obs_key 判断 + try-except 回退本地

## 八、知识交付方式——OBS URL + 三级降级（统一结构）

### 背景

当前方式：服务端读知识库内容 → 打成 JSON `{rel_path: content}` → 存到 agent_tasks 表 → Worker 解析 JSON → 落地到本地文件。

问题：知识库内容大时 JSON 膨胀 → agent_tasks 表字段太大 → claim 查询变慢。集群后 Worker 和服务端不在同一台机器，但 Worker 有 OBS AK/SK 可以直接访问 OBS 桶。

### 方案：统一结构 + 三级降级

把 knowledge_files 从 `{rel_path: content}` 改为 `{rel_path: {content, obs_url, obs_key}}`，Worker 按优先级取。

**统一结构：**
```json
knowledge_files = {
    "project-info/knowledge.md": {
        "content": "知识正文...",           // 兜底
        "obs_url": "https://obs.xxx/...",    // 优先用（temporary_url，有效期 1 小时）
        "obs_key": "knowledge/xxx/yyy.md"    // 直连 OBS 用（read_bytes）
    },
    "project-info/member/user1/design.md": {
        "content": "设计文档...",
        "obs_url": "https://obs.xxx/...",
        "obs_key": "knowledge/xxx/member/user1/design.md"
    }
}
```

**服务端 enqueue_task 打包逻辑：**
```python
for rel_path, content in knowledge_files(repo_key, agent_type, issue_num).items():
    obs_key = knowledge_obs_key(repo_key, scope, ...)
    obs_url = ""
    if obs_key and storage:
        obs_url = storage.temporary_url(obs_key, expires=3600)  # 1 小时有效
    knowledge_files[rel_path] = {
        "content": content,        # 兜底
        "obs_url": obs_url,        # 优先用
        "obs_key": obs_key or "",  # 直连 OBS
    }
```

**Worker 侧三级降级逻辑：**
```python
for rel_path, info in knowledge_files.items():
    content = None

    # 1. 优先 obs_url 下载
    url = info.get("obs_url", "")
    if url:
        try:
            content = http_download(url)  # aiohttp.get(url)
        except:
            content = None  # 下载失败，降级

    # 2. 降级到 content（服务端兜底打包）
    if not content:
        content = info.get("content", "")

    # 3. 降级到 Worker 直连 OBS 桶
    if not content:
        obs_key = info.get("obs_key", "")
        if obs_key:
            try:
                content = storage.read_bytes(obs_key)  # Worker 用 AK/SK 直连
            except:
                content = ""  # OBS 也读不到，空内容兜底

    # 4. 落地到本地文件
    write_file(know_dir / rel_path, content)
```

### 降级场景

| 场景 | obs_url | content | obs_key | Worker 用什么 | 说明 |
|------|:---:|:---:|:---:|------|------|
| 正常 | 有 | 有 | 有 | obs_url 下载 | 最常用，任务表轻量 |
| URL 过期 | 过期 | 有 | 有 | content | URL 1 小时后过期，Worker 用 content |
| URL + content 都空 | 空 | 空 | 有 | obs_key 直连 OBS | Worker 用 AK/SK 直读 OBS |
| OBS 也不可用 | 空 | 空 | 有 | 空内容 | 不崩溃 |
| OBS 未启用 | 空 | 有 | 空 | content | OBS 未配置时回退当前方式 |

### 向后兼容

老 Worker 只读 `info.get("content")` —— 不认识 obs_url / obs_key 时不影响：
```python
# 老 Worker 代码（向后兼容）
for rel_path, content in knowledge_files.items():
    if isinstance(content, str):       # 老格式 {rel_path: content}
        write_file(rel_path, content)
    else:                               # 新格式 {rel_path: {content, obs_url, obs_key}}
        write_file(rel_path, content.get("content", ""))
```

### 优势

| 对比 | 当前方式 | 新方式 |
|------|---------|--------|
| 任务表大小 | 存全部 content | 存 content + URL + key（content 兜底） |
| Worker 取内容 | 直接用 content | 优先 URL 下载，失败降级 |
| URL 过期 | 不适用 | 降级到 content |
| OBS 不可用 | 不适用 | 降级到空内容 |
| 网络 | Worker 不需网络 | Worker 需访问 OBS（有 AK/SK） |
| 兼容性 | — | 老 Worker 只读 content 不受影响 |

### 改动文件（更新第七节）

| # | 文件 | 改动内容 |
|---|------|---------|
| 3 | knowledge.py | knowledge_files() 返回值从 `{rel_path: content}` 改为 `{rel_path: {content, obs_url, obs_key}}` |
| 6 | runtime/service.py | enqueue_task 打包时生成 obs_url + obs_key |
| — | runtime/agent.py | Worker 侧三级降级逻辑 |

### project_info_files 也走同样结构

project_info_files（基线文件 project-graph.md 等）也改为统一结构：
```json
project_info_files = {
    "project-graph.md": {
        "content": "...",
        "obs_url": "https://obs.xxx/...",
        "obs_key": "project-info/xxx/project-graph.md"
    }
}
```

Worker 侧 project_info_files 和 knowledge_files 用同一个降级逻辑。

## 九、不需要改动的

- src/prompt_builder.py — 调 read_knowledge() / member_files()，随底层改造自动生效
- src/runtime/agent.py（worker 侧）— knowledge_files JSON 随任务下发，worker 落地逻辑不变
- 现有 save_bytes / read_bytes / delete_object / temporary_url — 够用，不需改

## 十、兼容策略

读取统一逻辑（伪代码）：
```python
if obs_key and storage:
    try:
        content = storage.read_bytes(obs_key)
        if content is None and file_path:
            content = open(file_path).read()  # OBS 读不到回退本地
    except Exception:
        content = open(file_path).read() if file_path else ""
elif file_path:
    content = open(file_path).read()
else:
    content = ""
```

storage 为 None（OBS 未启用）时全部回退本地，与 skill 存储改造同模式。

## 十一、数据迁移（渐进式）

不强制迁移现有数据。策略 A（渐进）：
- 老数据 obs_key 为空继续走本地
- 新数据写 OBS
- 随使用自然淘汰

零风险。

## 十二、风险点

### 1. write_knowledge 并发写

OBS 无 append，用读-合并-覆盖写。两个 agent 同时沉淀知识可能覆盖。

本地模式 open("a") 是原子追加，OBS 的读-合并-覆盖不是原子——OBS 模式并发风险比本地大。

但知识文件写入频率低（Agent 完成任务时才写一次），实际并发概率很小。可接受。

### 2. _member_docs 的 lastModified 格式

OBS 返回 "2026/08/23 05:34:43"，需要转成 "2026-08-23 05:34:43"。

### 3. list_objects_with_meta 性能

知识文件数量少（每个 repo 几个到十几个），一次调用即可，不存在性能问题。

### 4. OBS 读取失败回退

OBS 网络异常时回退本地 file_path。如果本地文件也不存在（容器化部署没有工作区），返回空字符串——Worker 拿不到知识但不崩溃。

## 十三、数据库新增字段

| # | 表.字段 | 类型 | 默认值 | 用途 |
|---|---------|------|--------|------|
| 1 | agent_shared_knowledge.obs_key | VARCHAR(512) | '' | OBS 对象键——非空走 OBS，空走本地 |
| 2 | agent_shared_knowledge.content_hash | VARCHAR(64) | '' | 内容 hash——缓存一致性比对 |

## 十四、项目知识基线文件 OBS 改造（补充）

### 背景

项目知识基线文件（project-graph.md / project-capabilities.md / .metadata.json）不在 agent_shared_knowledge 表里——它们由服务端 `src/project_info/builder.py` 的 `build_project_info()` 生成，直接写到工作区 `project-info/` 目录。

集群后本地文件不可共享——需要也走 OBS + 落库。

### 方案：新增 scope=baseline 落到 agent_shared_knowledge 表

不新建表——基线文件复用 agent_shared_knowledge 表，新增 scope=baseline：

| scope | agent_type | issue_num | file_path | 含义 |
|-------|-----------|-----------|-----------|------|
| project | 空 | 0 | project-info/knowledge.md | 项目级共享知识（Agent 沉淀） |
| issue | 空 | N | issues/{N}/knowledge.md | Issue 级共享知识 |
| issue_agent | agent_type | N | issues/{N}/{agent}/knowledge.md | Agent 级共享知识 |
| member | 空 | 0 | project-info/member/{user}/{file} | 成员上传文档 |
| **baseline** | 空 | 0 | project-info/project-graph.md | 项目知识基线（VOD 生成） |
| **baseline** | 空 | 0 | project-info/project-capabilities.md | 项目知识基线（VOD 生成） |
| **baseline** | 空 | 0 | project-info/.metadata.json | 项目知识基线元数据 |

每个 repo 的基线有 3 条记录（graph / capabilities / metadata），统一走 agent_shared_knowledge 表 + obs_key 字段。

baseline 字段值：
- scope = baseline
- repo_key = gitcode:xxx/xxx
- agent_type = ''（基线不分 Agent）
- issue_num = 0（基线不关联 Issue）
- file_path = project-info/project-graph.md（相对路径）
- obs_key = project-info/{repo_key}/project-graph.md（OBS key）
- status = accepted
- source_seq = 0
- created_by = vod

### OBS 对象键设计

```
project-info/{repo_key}/project-graph.md
project-info/{repo_key}/project-capabilities.md
project-info/{repo_key}/.metadata.json
```

### 改造点

#### 1. src/project_info/builder.py（生成→写入 OBS + 落库）

`_write_file()` 和 `_write_meta()` 改造：

```
OBS 模式（storage 不为 None）：
  1. obs_key = f"project-info/{repo_key}/{filename}"
  2. storage.save_bytes(obs_key, content.encode())
  3. INSERT INTO agent_shared_knowledge (scope, repo_key, file_path, obs_key, status, created_by)
     VALUES ('baseline', repo_key, rel_path, obs_key, 'accepted', 'vod')
  4. 同时写本地（兼容老逻辑）—— open(w) 保留

本地模式（storage 为 None）：
  1. 走现有 open(w) 写本地
  2. INSERT INTO agent_shared_knowledge (scope, repo_key, file_path, obs_key, ...)
     VALUES ('baseline', repo_key, rel_path, '', 'accepted', 'vod')
```

注意：基线文件每次重新生成时需要先 DELETE 旧的 baseline 记录再 INSERT 新的（或 INSERT OR REPLACE）。

#### 2. src/runtime/service.py（读取→查 DB + 走 OBS）

`load_project_info()` 改造：

```
OBS 模式：
  1. SELECT file_path, obs_key FROM agent_shared_knowledge
     WHERE repo_key=? AND scope='baseline' AND status='accepted'
  2. 对每条记录：
     - obs_key 非空 → storage.read_bytes(obs_key) + try-except 回退本地
     - obs_key 为空 → open(file_path) 读本地

本地模式：
  1. 走现有 open(r) 读本地
```

#### 3. src/im/vod_handler_async.py（VOD 注入→查 DB + 走 OBS）

第 203 行读 project-graph.md / project-capabilities.md：

```
OBS 模式：
  1. SELECT obs_key FROM agent_shared_knowledge
     WHERE repo_key=? AND scope='baseline' AND file_path LIKE '%project-graph.md'
  2. storage.read_bytes(obs_key) + 回退本地

本地模式：
  1. 走现有 open(r)
```

### 好处

- 统一模式——所有知识文件都在 agent_shared_knowledge 表
- 有 DB 索引——可以查询哪些 repo 有基线
- 有 obs_key——OBS 改造和共享知识/成员文档同模式
- 不需要新建表
- 前端 Knowledge 页面可以展示 baseline 条目

### 兼容策略

与共享知识同模式：
- OBS 模式：先读 OBS，失败回退本地
- 本地模式：走现有逻辑
- 老数据：本地文件继续可用，新生成的同时写 OBS + 落库

### 改动文件清单（补充）

| # | 文件 | 改动内容 |
|---|------|---------|
| 7 | src/project_info/builder.py | _write_file / _write_meta 加 OBS 写入 + INSERT baseline 记录 |
| 8 | src/runtime/service.py | load_project_info 查 DB baseline + OBS 读取 + 回退本地 |
| 9 | src/im/vod_handler_async.py | VOD 注入读基线查 DB + OBS 读取 + 回退本地 |
