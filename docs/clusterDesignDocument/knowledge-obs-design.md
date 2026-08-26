# 知识库 OBS 存储改造方案

## 背景

当前知识库（agent_shared_knowledge）正文存在工作区本地文件，多实例集群部署后本地文件不可共享。需要改造为 OBS 存储，与 skill 表的 OBS 改造同模式。

## 一、DB 变更

agent_shared_knowledge 表新增 1 个字段：

```
obs_key VARCHAR(512) DEFAULT ''
```

迁移语句（加到 db_setup.py migrations 列表）：
```sql
ALTER TABLE agent_shared_knowledge ADD COLUMN obs_key VARCHAR(512) DEFAULT ''
```

DDL 同步修改（新库建表时包含 obs_key）。

向前兼容逻辑：
- obs_key 非空 → 走 OBS（storage.read_bytes(obs_key)）
- obs_key 为空 → 走本地（open(file_path)，现有数据不破坏）
- storage 为 None（OBS 未启用）→ 全部回退本地

与 skill 表完全同模式：skill 用 skill_path（本地）+ file_path（OBS），knowledge 用 file_path（本地）+ obs_key（OBS）。

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

## 八、不需要改动的

- src/prompt_builder.py — 调 read_knowledge() / member_files()，随底层改造自动生效
- src/runtime/agent.py（worker 侧）— knowledge_files JSON 随任务下发，worker 落地逻辑不变
- 现有 save_bytes / read_bytes / delete_object / temporary_url — 够用，不需改

## 九、兼容策略

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

## 十、数据迁移（渐进式）

不强制迁移现有数据。策略 A（渐进）：
- 老数据 obs_key 为空继续走本地
- 新数据写 OBS
- 随使用自然淘汰

零风险。

## 十一、风险点

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

## 十二、数据库新增字段

| # | 表.字段 | 类型 | 默认值 | 用途 |
|---|---------|------|--------|------|
| 1 | agent_shared_knowledge.obs_key | VARCHAR(512) | '' | OBS 对象键——非空走 OBS，空走本地 |
