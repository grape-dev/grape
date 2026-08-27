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
- 读取时查 DB hash 对比本地缓存 hash → 一样用缓存 / 不一样从存储重新拉
- 保证集群多实例缓存一致性——DB hash 变了所有实例立即发现

updated_at 作用：
- 记录最后更新时间，排查用
- DDL 加 updated_at DATETIME DEFAULT CURRENT_TIMESTAMP

本地缓存方案（hash 比对）：
```python
_knowledge_cache: dict[str, tuple[str, str]] = {}  # {storage_key: (content, hash)}

def read_with_cache(storage_key, db_hash):
    cached = _knowledge_cache.get(storage_key)
    if cached and cached[1] == db_hash:
        return cached[0]  # hash 一样，用缓存
    content = storage.read_bytes(storage_key)  # hash 不一样或没缓存，从存储拉
    _knowledge_cache[storage_key] = (content, db_hash)
    return content
```
- 缓存命中：0.13ms（DB hash 查询 + hash 对比）vs 10-50ms（存储网络读取）→ 快 100 倍
- 集群一致性：实例 A 写知识库 → DB hash 更新 → 实例 B 下次读发现 hash 不一样 → 从存储重新拉

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

## 八、知识交付方式——文件 URL + 三级降级（通用设计，不绑死 OBS）

### 背景

当前方式：服务端读知识库内容 → 打成 JSON `{rel_path: content}` → 存到 agent_tasks 表 → Worker 解析 JSON → 落地到本地文件。

问题：知识库内容大时 JSON 膨胀 → agent_tasks 表字段太大 → claim 查询变慢。集群后 Worker 和服务端不在同一台机器，但 Worker 有存储 AK/SK 可以直接访问存储桶。

### 方案：字段分开存 + 三级降级（通用设计）

现有 `project_info_files` / `knowledge_files` 保持 `{rel_path: content}` 不变 → 老 Worker 不受影响。

新增 2 个字段存文件 URL + storage_key（通用命名，不绑死 OBS）：

**通用结构：**
```json
project_info_file_urls = {
    "owner/repo/project-info/project-graph.md": {
        "url": "https://obs.xxx/...",         // 优先用（HTTP 下载地址，当前是 OBS temporary_url）
        "storage_key": "project-info/xxx/project-graph.md"  // 直连存储用（当前是 OBS key）
    }
}

knowledge_file_urls = {
    "owner/repo/project-info/knowledge.md": {
        "url": "https://obs.xxx/...",
        "storage_key": "knowledge/xxx/knowledge.md"
    }
}
```

- `url` —— 任何 HTTP 下载地址（当前是 OBS temporary_url，以后可以是 MinIO / OSS 的 URL）
- `storage_key` —— 存储对象键（当前是 OBS key，以后可以是任意存储的 key）

**服务端 enqueue_task 打包逻辑：**
```python
# project_info_file_urls
project_info_file_urls = {}
for rel_path, content in project_info_files.items():
    storage_key = knowledge_obs_key(repo_key, scope, ...)
    url = ""
    if storage_key and storage:
        url = storage.temporary_url(storage_key, expires=3600)  # 1 小时有效
    project_info_file_urls[rel_path] = {
        "url": url,
        "storage_key": storage_key or "",
    }

# knowledge_file_urls（同上）
knowledge_file_urls = {}
for rel_path, content in knowledge_files.items():
    storage_key = knowledge_obs_key(repo_key, scope, ...)
    url = ""
    if storage_key and storage:
        url = storage.temporary_url(storage_key, expires=3600)
    knowledge_file_urls[rel_path] = {
        "url": url,
        "storage_key": storage_key or "",
    }
```

**Worker 侧三级降级逻辑（通用，不绑死 OBS）：**
```python
def _resolve_file_content(rel_path, content, file_urls, stats):
    """三级降级：url 下载 → content 兜底 → storage_key 直连存储。
    stats 收集降级统计用于日志上报。
    """
    meta = file_urls.get(rel_path, {})

    # 1. 优先 url 下载（HTTP GET，通用）
    url = meta.get("url", "")
    if url:
        try:
            downloaded = http_download(url)  # urllib/aiohttp
            if downloaded:
                return downloaded
        except:
            log.warning(f"[knowledge] url download failed: rel={rel_path}, url={url[:50]}")
            stats["url_fail"] += 1  # 降级统计

    # 2. 降级到 content（服务端兜底打包）
    if content:
        return content

    # 3. 降级到 Worker 直连存储（storage_key + 统一存储接口）
    storage_key = meta.get("storage_key", "")
    if storage_key:
        try:
            return storage.read_bytes(storage_key)  # 统一接口
        except:
            log.error(f"[knowledge] storage read failed: rel={rel_path}, key={storage_key}")
            stats["storage_fail"] += 1

    # 4. 空内容
    log.error(f"[knowledge] all fallback failed, empty content: rel={rel_path}")
    stats["empty"] += 1
    return ""

# 批量降级：先收集所有需要直连存储的文件，一次并发读取
async def _batch_resolve(know_files, know_urls, stats):
    """收集 URL 下载失败 + content 为空的文件，批量并发读取存储"""
    failed = {}  # {rel_path: storage_key}
    for rel_path, content in know_files.items():
        meta = know_urls.get(rel_path, {})
        url = meta.get("url", "")
        # URL 下载失败或没有 URL
        downloaded = None
        if url:
            try:
                downloaded = http_download(url)
            except:
                log.warning(f"[knowledge] url download failed: rel={rel_path}")
                stats["url_fail"] += 1
        if downloaded:
            continue  # URL 下载成功
        if content:
            continue  # content 不为空
        # 降级到 storage_key
        storage_key = meta.get("storage_key", "")
        if storage_key:
            failed[rel_path] = storage_key

    # 批量并发读取存储
    if failed:
        results = await storage.read_batch(list(failed.values()))
        success = sum(1 for v in results.values() if v)
        log.info(f"[knowledge] batch read: {success}/{len(failed)} succeeded")
        for rel_path, key in failed.items():
            content = results.get(key, "")
            if not content:
                log.error(f"[knowledge] storage read failed: rel={rel_path}, key={key}")
                stats["storage_fail"] += 1
        return results
    return {}

# 主逻辑
stats = {"url_fail": 0, "storage_fail": 0, "empty": 0}
proj_urls = json.loads(task.get("project_info_file_urls") or "{}")
know_urls = json.loads(task.get("knowledge_file_urls") or "{}")

# 先批量处理降级文件
proj_batch = await _batch_resolve(proj_files, proj_urls, stats)
know_batch = await _batch_resolve(know_files, know_urls, stats)

# 逐个落地
for rel, content in proj_files.items():
    if content:
        write_file(proj_dir / rel, content)
    elif proj_batch.get(proj_urls.get(rel, {}).get("storage_key", "")):
        write_file(proj_dir / rel, proj_batch[proj_urls[rel]["storage_key"]])
    else:
        write_file(proj_dir / rel, "")
        stats["empty"] += 1

for rel, content in know_files.items():
    if content:
        write_file(know_dir / rel, content)
    elif know_batch.get(know_urls.get(rel, {}).get("storage_key", "")):
        write_file(know_dir / rel, know_batch[know_urls[rel]["storage_key"]])
    else:
        write_file(know_dir / rel, "")
        stats["empty"] += 1

# 上报降级统计到任务日志
total_degraded = stats["url_fail"] + stats["storage_fail"] + stats["empty"]
if total_degraded > 0:
    log.warning(f"[knowledge] degraded: url_fail={stats['url_fail']}, "
                f"storage_fail={stats['storage_fail']}, empty={stats['empty']}")
    report_messages([{
        "type": "text",
        "content": f"⚠️ 知识文件降级：URL下载失败 {stats['url_fail']} 个，"
                   f"存储直连 {stats['storage_fail']} 个，空内容 {stats['empty']} 个"
    }])
```

### 日志和上报

**日志级别：**

| 场景 | 级别 | 格式 |
|------|------|------|
| URL 下载失败 | warning | `[knowledge] url download failed: rel={rel_path}` |
| 存储直连失败 | error | `[knowledge] storage read failed: rel={rel_path}, key={key}` |
| 全部失败（空内容） | error | `[knowledge] all fallback failed, empty content: rel={rel_path}` |
| 批量读取完成 | info | `[knowledge] batch read: {success}/{total} succeeded` |
| 降级统计汇总 | warning | `[knowledge] degraded: url_fail=N, storage_fail=N, empty=N` |

**上报到任务日志（task_messages）：**

降级次数 > 0 时，通过 report_messages 上报降级统计到任务日志，服务端在事件对话里可见：

```
⚠️ 知识文件降级：URL下载失败 2 个，存储直连 1 个，空内容 0 个
```

### 批量读取

降级到第 3 级（Worker 直连存储）时，先收集所有失败的 storage_key，一次并发读取：

```python
# storage 新增 read_batch 方法
async def read_batch(self, keys: list[str]) -> dict[str, str]:
    """批量并发读取——总耗时 = max(单个) 而不是 sum"""
    async def _read(key):
        try:
            return key, self.read_bytes(key)
        except:
            return key, ""
    results = await asyncio.gather(*[_read(k) for k in keys])
    return dict(results)
```

| 对比 | 逐个读取 | 批量读取 |
|------|---------|---------|
| 5 个失败 | 5 次串行 50-250ms | 1 批并发 10-50ms |
| 网络请求 | N 次 | 1 批并发 |
| 总耗时 | sum(单个) | max(单个) |

### 降级场景

| 场景 | url | content | storage_key | Worker 用什么 | 说明 |
|------|:---:|:---:|:---:|------|------|
| 正常 | 有 | 有 | 有 | url 下载 | 最常用 |
| URL 过期 | 过期 | 有 | 有 | content | URL 1 小时后过期 |
| URL + content 都空 | 空 | 空 | 有 | storage_key 直连存储 | Worker 用 AK/SK 直读 |
| 存储也不可用 | 空 | 空 | 有 | 空内容 | 不崩溃 |
| 存储未启用 | 空 | 有 | 空 | content | 回退当前方式 |

### 向后兼容

老 Worker 只读 `project_info_files` / `knowledge_files` → 不读 `file_urls` → 不受影响。

| 服务端 | Worker | 结果 | 说明 |
|--------|--------|:---:|------|
| 老格式 | 老 Worker | ✅ | 直接用 content |
| 老格式 | 新 Worker | ✅ | content 不为空直接用 |
| 新格式 | 老 Worker | ✅ | 只读 content 不受影响 |
| 新格式 | 新 Worker | ✅ | 三级降级 |

4 种场景全部兼容，无部署顺序要求。

### 通用性（不绑死 OBS）

| 层 | 当前实现 | 换存储时改什么 |
|----|---------|--------------|
| DB 字段名 | file_urls / url / storage_key | 不改（通用命名） |
| 服务端 | storage.temporary_url() | 换 storage driver |
| Worker 下载 | HTTP GET url | 不改（通用 HTTP） |
| Worker 直连 | storage.read_bytes(key) | 换 storage driver |

换存储（比如 MinIO / 阿里云 OSS）时只换 `src/storage/` 下的 driver，字段名和 Worker 逻辑不用改。

### 优势

| 对比 | 当前方式 | 新方式 |
|------|---------|--------|
| 任务表大小 | 存全部 content | content 兜底 + URL + key（content 可后续优化去掉） |
| Worker 取内容 | 直接用 content | 优先 URL 下载，失败降级 |
| 存储 | 绑死本地文件 | 通用存储接口，不绑死 OBS |
| 兼容性 | — | 老 Worker 不受影响 |
| 扩展性 | — | 换存储只换 driver |

### 改动文件（更新第七节）

| # | 文件 | 改动内容 |
|---|------|---------|
| 3 | knowledge.py | 新增 knowledge_obs_key() 生成 storage_key |
| 6 | runtime/service.py | enqueue_task 打包时生成 file_urls（url + storage_key） |
| — | runtime/agent.py | Worker 侧三级降级逻辑（通用，_resolve_file_content） |
| — | db_setup.py | agent_tasks DDL + migration 加 file_urls 字段 |

### DB 新增字段

| # | 表.字段 | 类型 | 默认值 | 用途 |
|---|---------|------|--------|------|
| 1 | agent_tasks.project_info_file_urls | TEXT | '' | JSON: {rel_path: {url, storage_key}} |
| 2 | agent_tasks.knowledge_file_urls | TEXT | '' | JSON: {rel_path: {url, storage_key}} |

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

## 十一、老数据迁移（后台定时同步）

### 背景

OBS 改造后新数据写入时同时写 OBS + DB obs_key。但老数据 obs_key 为空——需要同步到 OBS。

### 方案：后台定时同步

不需要启动时同步（数据多影响启动性能），不需要懒同步（读操作不应该有写副作用），不需要手动触发（当前不需要管理员操作）。

| 时机 | 触发 | 做什么 |
|------|------|--------|
| 后台定时 | 每小时 | 扫描 obs_key 为空的行（LIMIT 500）→ 读本地文件 → 写 OBS → 更新 obs_key + content_hash |

**实现逻辑：**
```python
async def knowledge_sync_loop():
    """后台定时同步老数据到 OBS——每小时一次，分批 500 条"""
    while True:
        try:
            cursor = 0
            while True:
                with db_lock:
                    c = db.cursor()
                    rows = c.execute(
                        "SELECT id, file_path, scope, repo_key, agent_type, issue_num "
                        "FROM agent_shared_knowledge WHERE obs_key='' AND id>? ORDER BY id LIMIT 500",
                        (cursor,)
                    ).fetchall()
                if not rows:
                    break
                for row in rows:
                    content = read_local_file(row["file_path"])
                    if content:
                        storage_key = knowledge_obs_key(row["repo_key"], row["scope"], ...)
                        storage.save_bytes(storage_key, content)
                        content_hash = hashlib.sha256(content.encode()).hexdigest()[:64]
                        with db_lock:
                            c.execute(
                                "UPDATE agent_shared_knowledge SET obs_key=*** content_hash=? WHERE id=***",
                                (storage_key, content_hash, row["id"])
                            )
                            db.commit()
                    cursor = row["id"]
                if len(rows) < 500:
                    break  # 没有更多了
            # 检查是否还有未同步的
            with db_lock:
                remaining = c.execute(
                    "SELECT COUNT(*) FROM agent_shared_knowledge WHERE obs_key=***"
                ).fetchone()[0]
            if remaining == 0:
                log.info("[knowledge] sync complete, all data migrated to OBS")
                return  # 全部同步完，停止任务
        except Exception as e:
            log.error(f"[knowledge] sync error: {e}")
        await asyncio.sleep(3600)  # 1 小时
```

**特性：**
- 每小时跑一次，每次最多 500 条（游标分页，和 lease sweep 同模式）
- 不影响启动——后台 asyncio task
- 跑完（没有 obs_key 为空的行）自动停止
- 同步时计算 content_hash 一并写入 DB

**老数据 obs_key 为空时 Worker 怎么处理：**
- Worker 拿不到 url / storage_key → 直接用 content 兜底（当前方式）
- 不影响功能——老数据照样能通过 content 下发给 Worker
- 后台同步完成后自动切换到 OBS 读取

### 改动文件

| # | 文件 | 改动 |
|---|------|------|
| — | src/api/knowledge.py | 新增 knowledge_sync_loop() |
| — | src/api/main.py | lifespan 启动 knowledge_sync_loop 后台任务 |

零风险——老数据 obs_key 为空时 Worker 用 content 兜底，不影响功能。

## 十二、风险点

### 1. write_knowledge 并发写

OBS 无 append，用读-合并-覆盖写。两个 agent 同时沉淀知识可能覆盖。

本地模式 open("a") 是原子追加，OBS 的读-合并-覆盖不是原子——OBS 模式并发风险比本地大。

但知识文件写入频率低（Agent 完成任务时才写一次），实际并发概率很小。接受风险 + 并发写时 log warning。

### 2. _member_docs 的 lastModified 格式

list_objects_with_meta 返回的 lastModified 是 OBS 的时间戳格式（RFC 1123），前端可能期望 Unix timestamp 或 ISO 8601。

修复：list_objects_with_meta 统一返回 ISO 8601 格式。

### 3. list_objects_with_meta 性能

OBS list_objects 每次最多 1000 条，成员文档多时需要分页。

修复：list_objects_with_meta 支持分页，cursor 推进直到查完。

### 4. OBS 读取失败回退

OBS 网络异常时回退本地 file_path。如果本地文件也不存在（容器化部署没有工作区），返回空字符串——Worker 拿不到知识但不崩溃。

### 5. member_files 重复打包

现有设计：member_files() 在两个地方被打包：
- enqueue_task 里 member_files_pi() → 并入 project_info_files → Worker 落地到 .project-info/member/
- knowledge_files() 里 member_files() → 并入 knowledge_files → Worker 落地到 .knowledge/owner/repo/.../member/

两个函数读的是同一个目录 `workspace/{owner}/{repo}/project-info/member/`，只是 rel_path 格式不同。

prompt 引导 Agent 读 `{{PROJECT_INFO_DIR}}/member/`，没有引导读 `{{KNOWLEDGE_DIR}}/member/`。第 2 个打包可能是无效的，但不确定是历史遗留还是兜底设计，贸然去掉有风险。

OBS 改造处理：不去掉 member_files()（保持现有行为），但 OBS 只存一份——member_files 和 member_files_pi 共用同一个 storage_key。project_info_file_urls 和 knowledge_file_urls 里指向同一个 OBS 对象，Worker 两次下载同一个 URL（第二次走本地缓存）。

### 6. temporary_url 1 小时过期

enqueue_task 每次创建任务时重新生成 temporary_url（有效期 1 小时）。任务排队超过 1 小时时 URL 过期。

影响不大——任务排队超过 1 小时的场景很少（Worker 空闲就 claim）。即使过期了还有 content 兜底，不会丢数据。接受降级。

### 7. delete_knowledge OBS 删失败

delete_object 失败时 OBS 里文件还在，如果 DB obs_key 已清空 → 残留孤儿对象。

修复：先删 OBS 再清 DB——OBS 删失败时 DB 不清 obs_key，下次同步任务重试删除。log warning 记录失败。

### 8. 后台同步和正常写入的并发

后台同步任务读本地文件 → 写 OBS → 更新 DB obs_key。同时 Agent 调 write_knowledge 写本地文件 → 写 OBS → 更新 DB obs_key。

两个都通过 db_lock 保护——不会并发写同一行。同步任务和 write_knowledge 不会同时操作同一行。

## 十三、数据库新增字段

| # | 表.字段 | 类型 | 默认值 | 用途 |
|---|---------|------|--------|------|
| 1 | agent_shared_knowledge.obs_key | VARCHAR(512) | '' | 存储对象键——非空走存储，空走本地 |
| 2 | agent_shared_knowledge.content_hash | VARCHAR(64) | '' | 内容 hash——缓存一致性比对 |
| 3 | agent_shared_knowledge.updated_at | DATETIME | CURRENT_TIMESTAMP | 最后更新时间，排查用 |
| 4 | agent_tasks.project_info_file_urls | TEXT | '' | JSON: {rel_path: {url, storage_key}} |
| 5 | agent_tasks.knowledge_file_urls | TEXT | '' | JSON: {rel_path: {url, storage_key}} |

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
