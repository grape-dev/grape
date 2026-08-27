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
    """批量并发读取——总耗时 = max(单个) 而不是 sum。
    限制并发数防止触发存储限流。
    """
    sem = asyncio.Semaphore(10)  # 最多 10 个并发

    async def _read(key):
        async with sem:
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
| 100 个失败 | 100 次串行 1-5s | 10 批并发 0.1-0.5s |
| 并发限制 | 无 | Semaphore(10) 防止限流 |
| 网络请求 | N 次 | 1 批并发（最多 10） |
| 总耗时 | sum(单个) | max(单个) × ceil(N/10) |

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
| 6 | agent_runtimes.worker_version | TEXT | '' | Worker 代码版本号 |

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

### OBS 写失败处理

builder.py 生成基线文件时写 OBS + 落库。OBS 写失败时：
- 只写本地文件 + log warning
- DB obs_key 为空 → 和老数据同样的降级逻辑
- 后台同步任务每小时迁移（第十一节）

**注意点（7 个）：**

1. **后台线程执行**——builder.py 通过 `threading.Thread` 后台执行，OBS 写失败时 log warning 不影响 webhook 响应 ✅

2. **并发防重**——`maybe_refresh_project_info` 用 `_lock_guard` + `_building` set 防重，同一个 repo_key 不会并发执行 ✅

3. **OBS 写失败后本地文件已写入**——builder.py 先写本地文件再写 OBS，OBS 失败时本地文件已经有了，obs_key 为空走本地读取 ✅

4. **重复执行**——issue_updated 再次触发 builder.py 时覆盖本地文件 + OBS 重写，OBS 写成功后 DB obs_key 有值 ✅

5. **同步任务和 builder 同时执行**——后台同步任务可能读到 builder.py 写了一半的文件。`_write_file` 用 `open("w")` 写入几毫秒完成，同步任务每小时跑一次，时间窗口极小。同步任务有 try-except 保护，读失败跳过下一轮再读。可接受。

6. **DB INSERT 失败**——OBS 写成功但 DB 写失败会产生孤儿对象（OBS 有文件 DB 没记录）。MySQL 在本地，写失败概率极低。后续可加定期清理任务扫描孤儿对象。

7. **content_hash 计算时机**——写 OBS 时同时计算 hash 存 DB。OBS 写失败时不计算 hash（不写 DB）。DB 写失败时 hash 丢失但 OBS 有文件，后台同步任务迁移时重新计算 hash ✅

### baseline content_hash

baseline 文件写入时也计算 content_hash 存 DB：
- builder.py _write_file / _write_meta 写 OBS 时同时计算 hash
- 读取时走 hash 缓存（和共享知识同模式）

### 改动文件清单（补充）

| # | 文件 | 改动内容 |
|---|------|---------|
| 7 | src/project_info/builder.py | _write_file / _write_meta 加 OBS 写入 + content_hash + INSERT baseline 记录 + OBS 写失败降级本地 |
| 8 | src/runtime/service.py | load_project_info 查 DB baseline + OBS 读取 + hash 缓存 + 回退本地 |
| 9 | src/im/vod_handler_async.py | VOD 注入读基线查 DB + OBS 读取 + hash 缓存 + 回退本地 |

### Worker 版本号上报

Worker register 时上报 worker_version 到 agent_runtimes 表：

| # | 文件 | 改动内容 |
|---|------|---------|
| 10 | db_setup.py | agent_runtimes DDL + migration 加 worker_version TEXT DEFAULT '' |
| 11 | runtime/agent.py | main() 调 register 时传 cli_version=_read_local_worker_version() |
| 12 | api/routes/runtime.py | register 函数存 worker_version 到 DB |

当前已有基础：
- Worker 有 _read_local_worker_version() 读 /etc/grape/worker.version ✅
- register 请求体有 cli_version 字段 ✅
- 只是 main() 没传 + 服务端没存

### 测试用例

| # | 用例 | 测试步骤 | 预期结果 |
|---|------|---------|---------|
| TC-K01 | obs_key 为空走本地 | DB obs_key='' → read_knowledge | 读本地文件 |
| TC-K02 | obs_key 非空走 OBS | DB obs_key 非空 → read_knowledge | 走 OBS + hash 缓存 |
| TC-K03 | hash 缓存命中 | DB hash 和缓存 hash 一致 | 用缓存（0ms） |
| TC-K04 | hash 缓存未命中 | DB hash 和缓存 hash 不一致 | 从 OBS 重新拉 |
| TC-K05 | OBS 读取失败回退本地 | OBS 异常 → read_knowledge | 回退本地文件 |
| TC-K06 | storage=None 全部回退本地 | OBS 未启用 | 全部走本地 |
| TC-K07 | write_knowledge 写 OBS + DB | 调 write_knowledge | OBS 有文件 + DB 有 obs_key + content_hash |
| TC-K08 | upload_knowledge 写 OBS + DB | 上传文件 | OBS 有文件 + DB 有 obs_key |
| TC-K09 | delete_knowledge 先删 OBS 再清 DB | 删文件 | OBS 删成功 → DB 清空 |
| TC-K10 | delete_knowledge OBS 删失败 | OBS 删失败 | DB 不清 + log warning |
| TC-K11 | enqueue_task 生成 file_urls | 创建任务 | file_urls 有 url + storage_key |
| TC-K12 | Worker URL 下载成功 | Worker 下载 url | 拿到 content |
| TC-K13 | Worker URL 下载失败降级 content | URL 过期 | 用 content |
| TC-K14 | Worker content 为空降级 storage_key | content 为空 | 直连存储读取 |
| TC-K15 | Worker 批量读取 | 5 个文件降级 | 1 批并发 10-50ms |
| TC-K16 | 后台同步迁移老数据 | obs_key 为空 → 同步 | OBS 有文件 + DB 有 obs_key |
| TC-K17 | 后台同步跑完自动停止 | 没有 obs_key 为空 | 任务结束 |
| TC-K18 | baseline 写 OBS + 落库 | builder.py 生成 | OBS 有文件 + DB baseline 记录 |
| TC-K19 | baseline OBS 写失败降级本地 | OBS 写失败 | 只写本地 + log warning |
| TC-K20 | Worker 版本号上报 | register | DB worker_version 有值 |
| TC-K21 | read_batch 并发限制 | 100 个文件降级 | Semaphore(10) 限制 |
| TC-K22 | member_files 共用 storage_key | 两处打包同一个文件 | OBS 只存一份 |

### 监控告警

health_check 加知识库 OBS 统计：
- 后台同步任务状态（运行中/已完成/失败）
- obs_key 为空的行数（待迁移）
- 最近 1 小时降级次数（url_fail / storage_fail / empty）


## 十五、第五轮深度检查——5 个优化点（2026-08-27）

### 优化 1：file_urls 路径穿越修复 ❌ 必须修复

**问题**：generate_file_urls 生成双格式 key 时，`_os.path.relpath(rel_path, project-info 目录)` 在 rel_path 不在 project-info 目录下时产生 `../../../../` 路径穿越。

**影响**：Worker 落地时 `../../../../grape/xxx` 路径会写到 workdir 外面，存在安全风险。

**修复**：双格式 key 生成前检查 rel_path 是否在 project-info 目录下，不在则跳过不生成双格式 key。

### 优化 2：knowledge_files OBS 模式重复读取 ⚠️ 性能优化

**问题**：read_knowledge 已经从 OBS 拿到 content 并返回，但 knowledge_files 没用返回的 content，又 `open(p)` 读本地文件。OBS 模式下读了两遍。

**修复**：knowledge_files 直接用 read_knowledge 返回的 content，不再 open 读文件。本地模式行为不变（read_knowledge 本地模式也是 open 读文件返回 content）。

### 优化 3：enqueue_task 调两次 generate_file_urls ⚠️ 性能优化

**问题**：project_info_file_urls 和 knowledge_file_urls 调两次同样的 generate_file_urls，查 6 次 DB（每次 3 查）。生成结果完全一样。

**修复**：调一次 generate_file_urls，结果同时赋给 project_info_file_urls 和 knowledge_file_urls。

### 优化 4：sync_loop 跑完就停 ⚠️ 逻辑优化

**问题**：`remaining == 0 → return`。新增的 obs_key 为空的记录不会被处理。sync_loop 只在启动时跑一次。

**修复**：去掉 return，改为每小时继续跑（await asyncio.sleep(3600) 循环）。处理新增的老数据。

### 优化 5：read_with_cache 缓存 miss 后 hash 校验 ⚠️ 数据一致性

**问题**：从 OBS 拉到内容后计算 actual_hash，但没和 db_hash 比对。如果 OBS 内容和 DB hash 不一致不会发现。

**修复**：actual_hash != db_hash 时 log warning（不阻断返回——返回 OBS 内容兜底）。


## 十六、第六轮深度检查——OBS 模式下完全走 DB/OBS（2026-08-27）

### 问题

OBS 模式下部分函数仍然读本地磁盘，集群场景下其他实例没有本地文件会漏数据：

- `member_files()` — os.walk 本地 member 目录
- `member_files_pi()` — os.walk 本地 member 目录
- `agent_files()` — os.walk 本地 agent 目录
- `load_project_info()` — 读本地 project-graph.md / project-capabilities.md
- `generate_file_urls()` — 调 member_files() / agent_files() 遍历本地磁盘

### 修复方案

所有函数统一模式：`if storage: 查 DB + OBS 读取` `else: 读本地（和 dev 一致）`

| # | 文件 | 函数 | 改动 |
|---|------|------|------|
| 1 | knowledge.py | member_files() | 加 OBS 分支：查 DB scope='member' + read_bytes |
| 2 | knowledge.py | member_files_pi() | 加 OBS 分支：查 DB + read_bytes，rel_path 用 project-info 相对 |
| 3 | knowledge.py | agent_files() | 加 OBS 分支：查 DB scope='member' AND agent_type + read_bytes |
| 4 | knowledge.py | knowledge_files() | 恢复 open 读文件（修复 strip 差异，和 dev 一致） |
| 5 | knowledge.py | sync_loop COUNT | 创建新 cursor（修复 cursor 复用） |
| 6 | service.py | load_project_info() | 加 OBS 分支：查 DB scope='baseline' + read_bytes |
| 7 | obs.py | generate_file_urls() | member/agent 部分不调 member_files/agent_files，直接用 DB 查询结果 |

### 不改的（已经走 OBS）

- knowledge_path() — 路径计算函数
- read_knowledge() — 已加 OBS 分支
- write_knowledge() — 已加双写
- upload_knowledge() — 已加双写
- builder.py _write_to_obs() — 已加
- vod_handler_async.py — 已查 DB baseline
- _materialize_files() — Worker 侧三级降级


## 十七、第七~九轮深度检查——兜底/去重/清理（2026-08-27）

### 第七轮：兜底 + 去重

1. load_project_info OBS 分支查不到 baseline 时回退本地读取（兜底）
   - DB 查到 baseline → OBS 读取 → 返回
   - DB 查不到 → 继续走本地逻辑（和 dev 一致）
   
2. member_files/member_files_pi/agent_files DB 查询去重
   - 同一个 file_path 多条 DB 记录（upload_knowledge 每次上传 INSERT 一行）
   - 需要按 file_path 去重只保留最新 obs_key

### 第八轮：GROUP BY 改 Python 去重 + 扩展名过滤

1. GROUP BY file_path 的 obs_key 取值不确定（MySQL GROUP BY 非聚合列）
   - 改为 ORDER BY id DESC + Python 侧 seen set 去重
   - 确保取最新 id 的 obs_key

2. member_files/member_files_pi/agent_files OBS 分支加扩展名过滤
   - 本地模式只读 .md/.txt/.json
   - OBS 分支也加同样的扩展名过滤（行为一致）

3. generate_file_urls member/agent 同样改为 Python 去重
   - SELECT 加 id 列用于排序
   - Python 侧 seen set 去重

### 第九轮：无用 import 清理

1. obs.py 删掉无用 import member_files
   - generate_file_urls 改后不再调 member_files()
   - import 了但没用到 → 删掉

### 不修（dev 分支原有行为）

- member_files 返回含 agent 专属文档（重复打包）
  - dev 分支本地模式也遍历 agent 子目录
  - 不是 OBS 改造引入的
  - key 格式不同不会 dict 覆盖
  - Worker 多落一份文件——不影响功能


## 十八、第十轮深度检查——member_files 排除 agent 专属文档（2026-08-27）

### 问题

member_files / member_files_pi / generate_file_urls 的 OBS 分支查 DB `scope='member'` 时没有排除 `agent_type` 非空的记录：

- 本地模式 `os.walk(project-info/member/)` 只遍历 member 目录，不含 agent 目录
- OBS 分支查 DB `scope='member'` 返回了 `agent_type='coding'` 的记录（file_path 是 `project-info/agent/coding/xxx`）
- 行为不一致：本地模式返回 9 个文件，OBS 分支返回 11 个文件（多 2 个 agent 专属）

### 影响

OBS 模式下 agent 专属文档被打包两次：
1. knowledge_files 调 member_files → 返回含 agent 文件（WORKSPACE_DIR 相对 key）
2. enqueue_task 调 agent_files → 返回 agent 文件（project-info 相对 key）
key 不同不会 dict 覆盖 → Worker 落地两个不同路径的相同文件

### 修复

member_files / member_files_pi / generate_file_urls member 部分的 DB 查询加：
`AND (agent_type='' OR agent_type IS NULL)`

只返回普通 member 文件（agent_type 为空），不含 agent 专属文档（agent_type 非空）。
和本地模式行为一致。


## 十九、第十一轮深度检查——delete_knowledge 数据一致性（2026-08-27）

### 问题

delete_knowledge 删除顺序：
1. 查 DB obs_key → 非空先删 OBS（delete_object）
2. os.remove(full) 删本地文件
3. DELETE FROM 清 DB

OBS 删成功但本地文件删除失败时 raise 500 中止 → OBS 已删但 DB 还在 → 后续读取走 OBS 会 404。

### 修复

OBS 已删（obs_key 非空）时本地删除失败只 log warning 继续清 DB。
本地模式（obs_key 为空）仍 raise 500 中止。

### 不修

_read_file 查 DB 不限 scope——当前 file_path 格式不同不会冲突，影响低。


## 二十、第十二~十三轮深度检查——sync_loop 文件不存在优化（2026-08-27）

### 问题

sync_loop 处理 obs_key 为空的记录时，文件不存在的记录（老 repo 已删除）每次循环都会重新查到 → 反复尝试 → 浪费 DB 查询 + 日志。

### 修复

- 加 `_sync_not_found` 集合记录文件不存在的 rid
- 进入循环时先检查 rid 是否在集合中 → 跳过
- 文件不存在时 log error 告警 + 加入集合 + continue
- 不影响其他正常数据的处理
- 进程重启后集合清空（内存集合，不持久化）——重启后会重新尝试一次，仍然不存在则跳过

### 第十二~十三轮其他检查项

- cursor 使用：全部正确，无跨 db_lock 块复用 ✅
- write_knowledge rejected 场景：open 在 if accepted 内，不报错 ✅
- _read_entry_content file_path 格式：只处理 write_knowledge 的记录，格式正确 ✅
- upload_knowledge save_bytes：正确 ✅
- agent_files OBS 查询条件：正确 ✅


## 二十一、第十四轮深度检查——read_with_cache 缓存命中修复（2026-08-27）

### 问题

read_with_cache 缓存存的是 actual_hash（OBS 内容的 hash），但命中条件是 `cached[1] == db_hash`。

如果 actual_hash != db_hash（OBS 内容和 DB hash 不一致）：
- 每次都 miss → 每次都从 OBS 读 → 缓存形同虚设
- hash mismatch warning 每次都打

### 修复

缓存存 db_hash 而不是 actual_hash：
- 下次 DB hash 没变 → 命中（content 是上次从 OBS 拉的）
- DB hash 变了 → miss → 从 OBS 重新拉
- hash mismatch 仍然 log warning 但不会反复打

### 第十四轮其他检查项

- sync_loop except 块 ✅（文件不存在 + 读取失败都不影响其他数据）
- _sync_not_found 模块级 set ✅（单实例有效，重启清空）
- member_files OBS rel_path ✅（和本地模式格式一致）
- baseline key 一致性 ✅（VOD / generate_file_urls / load_project_info 三者一致）


## 二十二、最终验证——集群 OBS 模式全流程（2026-08-27）

### 部署环境

- A 服务：127.0.0.1:3001（OBS 模式 + Lease 集群）
- B 服务：127.0.0.1:3002（OBS 模式 + Lease 集群）
- nginx 80 负载均衡到 3001+3002
- MySQL：127.0.0.1:3306/rds-grape-dev
- OBS：obs-grape-dev-file 桶，prefix=grape/prod

### 验证结果

#### 全流程验证（11 个环节）

| # | 环节 | 结果 |
|---|------|------|
| 1 | 环境 | ObsStorage ✅ |
| 2 | 知识写入 | 双写（本地+OBS），DB obs_key+content_hash 有值 ✅ |
| 3 | OBS 读取 + 缓存 | 第一次 miss→OBS，第二次 hit→0ms，一致 ✅ |
| 4 | member_files OBS 分支 | 8 个（不含 agent），agent 泄漏=0 ✅ |
| 5 | member_files_pi OBS 分支 | 8 个 ✅ |
| 6 | agent_files OBS 分支 | 2 个 ✅ |
| 7 | knowledge_files | 9 个 ✅ |
| 8 | generate_file_urls | 23 个，有 URL=19，路径穿越=0 ✅ |
| 9 | load_project_info | 3 个文件（兜底本地）✅ |
| 10 | Worker 三级降级 | URL 有效→URL 下载 ✅ |
| 11 | 集群 Lease | A=8 connected, B=0, 隔离 OK ✅ |

#### 删除验证

删除 3 个 member 文件（BLOG_USE_BASE-1787435478.json / blog-config-1787820597.json / blog_pub-1787435356.json）：
- HTTP 200 OK ✅
- 本地文件已删 ✅
- DB 记录已删 ✅
- OBS 对象已删 ✅
- 删除顺序：先删 OBS → 再删本地 → 再清 DB ✅

#### 上传验证

上传 2 个文件（member/blog-config-1787833552.json + agent/issue/blog-config.json）：
- HTTP 200 OK ✅
- 本地文件已写 ✅
- DB 记录有 obs_key + content_hash ✅
- OBS 对象已写 ✅
- 集群负载均衡：A 处理 1 个，B 处理 1 个 ✅

#### Issue 任务知识注入验证

task=t_a3f650fa194a, agent=issue, status=running：
- knowledge_files: 7 个（删除后减少，上传后含新增）✅
- project_info_files: 10 个（含 agent/issue 文档）✅
- knowledge_file_urls: 18 个，有 URL=14，路径穿越=0 ✅
- 已删文件不在打包列表 ✅
- 新上传文件在打包列表 ✅

#### Agent 知识验证

agent_files OBS 分支：
- agent_files(issue): 1 个（agent/issue/blog-config.json）✅
- agent_files(coding): 2 个 ✅
- OBS 读取内容一致 ✅

### sync_loop 优化验证

- 文件不存在的记录（id=1, songspace/shelldev）：log error 后跳过，不重复处理 ✅
- _sync_not_found 集合生效 ✅

### 缓存验证

- read_with_cache 存 db_hash → 下次 DB hash 没变就命中 ✅
- hash mismatch 时 log warning 但不阻断 ✅

### 总结

15 轮深度检查 + 集群 OBS 模式全流程验证通过。代码实现符合设计文档：
- OBS 模式完全走 DB/OBS（不依赖本地磁盘）
- 本地模式保持和 dev 一致
- 向前兼容（所有新字段默认空值，storage=None 回退本地）
- 三级降级（URL → content → storage_key）
- hash 缓存 + LRU
- 集群 Lease 隔离
- 删除/上传/任务打包全链路数据一致


## 二十三、sync_loop 补扫 baseline 文件方案（2026-08-27）

### 问题

sync_loop 只扫 agent_shared_knowledge 表里 obs_key 为空的记录。baseline 文件（project-graph.md / project-capabilities.md / .metadata.json）在 OBS 代码之前 onboard 的 repo 没有 DB baseline 记录 → sync_loop 扫不到 → 不会迁移。

集群场景下 B 实例没有本地文件 → load_project_info 查 DB 查不到 → 回退本地也没有 → 基线缺失。

### 基线文件清单（builder.py 硬编码常量）

| 文件 | 常量 | 说明 |
|------|------|------|
| project-graph.md | _GRAPH_FILE | 项目架构图 |
| project-capabilities.md | _CAP_FILE | 项目能力描述 |
| .metadata.json | _META_FILE | 元数据（commit sha / branches / last_updated） |

### 修复方案

新增 `_sync_baseline_files()` 函数，在 sync_loop 外层循环末尾调用。

逻辑：
1. 遍历 WORKSPACE_DIR 下所有 repo 的 project-info 目录
2. 对每个 repo 检查 3 个基线文件（project-graph.md / project-capabilities.md / .metadata.json）
3. 查 DB 是否已有 scope='baseline' 记录
4. 没有记录 → 写 OBS + INSERT DB
5. 有记录但 obs_key 为空 → 写 OBS + UPDATE obs_key
6. 有记录且 obs_key 非空 → 跳过

### 改动点

| 文件 | 函数 | 改动 |
|------|------|------|
| knowledge.py | knowledge_sync_loop() | 外层循环末尾加 _sync_baseline_files() 调用 |
| knowledge.py | 新增 _sync_baseline_files() | 扫描本地 project-info + 写 OBS + DB |

### 设计原则

1. 和 sync_loop 同样的模式：查 DB → 没有就写 OBS + DB
2. 不依赖配置项——用硬编码的 BASELINE_FILES（和 builder.py 一致）
3. 幂等——DB 有 obs_key 非空的记录就跳过
4. 不影响现有 sync_loop 逻辑——在现有循环之后执行
5. 文件不存在就跳过——不报错

### 不改的

- builder.py _write_to_obs — 不变（新 onboard 的 repo 已经会写 DB）
- load_project_info — 不变（已有兜底逻辑）
- _PROJECT_INFO_FILES — 不变

### 方案调整（2026-08-27）

原方案遍历 WORKSPACE_DIR 目录名作为 owner，但目录名格式不一致（有的带 `gitcode:` 前缀）。

调整：从 `managed_repos` 表获取项目列表，不遍历目录。

managed_repos 表有 36 条记录，每条有 repo_key（如 `gitcod.../...`）和 repo_owner / repo_name。

调整后逻辑：
1. 查 DB: `SELECT repo_key FROM managed_repos` → 拿到所有项目的 repo_key
2. 对每个 repo_key:
   - owner, repo = split_owner_repo(repo_key)
   - pi_dir = WORKSPACE_DIR/owner/repo/project-info
3. 检查 pi_dir 下的 3 个基线文件
4. 查 DB baseline 记录 → 没有就写 OBS + INSERT / 有但 obs_key 空就 UPDATE / 有且非空跳过

### 方案检查结果（2026-08-27）

#### managed_repos 表结构

- 无软删除字段（is_deleted）——36 条记录全部有效
- 只有 1 个项目（xiaowu2119/hello-skill）有本地 project-info 目录
- 35 个项目没有目录——os.path.isdir 返回 False 跳过，不影响功能

#### storage_key 格式一致性

builder.py 的 _write_to_obs 用：
```python
storage_key = f"project-info/{repo_key}/{name}"
# → project-info/gitcode:xiaowu2119/hello-skill/project-graph.md
```

_sync_baseline_files 必须用同样的格式，确保 OBS 对象 key 一致。

#### 最终实现逻辑

1. 查 DB: `SELECT repo_key FROM managed_repos` → 拿到所有项目
2. 对每个 repo_key:
   - `owner, repo = split_owner_repo(repo_key)`
   - `pi_dir = WORKSPACE_DIR/owner/repo/project-info`
   - `os.path.isdir(pi_dir)` 为 False → 跳过
3. 对 pi_dir 下的 3 个基线文件（project-graph.md / project-capabilities.md / .metadata.json）:
   - 文件不存在 → 跳过
   - 查 DB: `SELECT id, obs_key WHERE repo_key=? AND scope='baseline' AND file_path=?`
   - DB 有记录且 obs_key 非空 → 跳过
   - DB 有记录但 obs_key 空 → 写 OBS + UPDATE obs_key
   - DB 无记录 → 写 OBS + INSERT
4. storage_key 格式：`f"project-info/{repo_key}/{fname}"`（和 builder.py 一致）
5. file_path 格式：`os.path.relpath(fpath, WORKSPACE_DIR)`（和 builder.py 一致）

#### 注意事项

- managed_repos 无软删除——35 个没目录的项目会跳过，不影响功能
- 幂等——DB 有 obs_key 非空的记录就跳过
- 每小时执行一次——和 sync_loop 同频率

### 并发保护（2026-08-27）

多实例同时启动 sync_loop 会导致重复写 OBS + DB。

方案 3：meta 表标记 + 10 分钟超时

- sync_loop 开始前查 meta 表 `knowledge_sync_running`
- 没有记录或时间戳超过 10 分钟 → 写入当前时间戳 → 执行
- 有记录且 10 分钟内 → 跳过本次（log info）
- sync_loop 完成后清空标记（finally 块）
- 实例挂了 10 分钟后自动恢复

优点：不加字段（用现有 meta 表）、不加锁、超时保护、和飞书 lease 思路一致。

### 并发保护验证证据（2026-08-27）

8 个验证全部通过：

1. 清空 baseline 记录 + meta 锁标记 → 3 条删除 + 锁清空 ✅
2. _sync_baseline_files 执行 → 3 个文件写入 OBS + DB ✅
3. DB baseline 记录 → 3 条，obs_key/content_hash 有值，created_by=sync ✅
4. OBS 对象 → 3 个对象，内容和 DB 记录对应 ✅
5. 幂等 → 第二次执行 baseline 记录还是 3 条，无新增 ✅
6. 并发保护 → A 抢锁 B 跳过 (elapsed=0.0s < 600s)，A 释放后 B 继续 ✅
7. 超时恢复 → 11 分钟前的锁过期 (660s > 600s) 继续执行 ✅
8. load_project_info → 3 个文件从 OBS 读取，内容正确 ✅

### 影响分析

7 个检查点全部通过，对系统无伤害：
- INSERT 字段和表结构匹配 ✅
- storage_key 格式和 builder.py 一致 ✅
- file_path 格式和 builder.py 一致 ✅
- finally set_meta_value 内部有 try/except 不抛异常 ✅
- managed_repos 查询无占位符问题 ✅
- meta 表临时标记 finally 清空 + 10 分钟超时 ✅
- 36 个项目遍历 35 次跳过微不足道 ✅

## 二十四、307 重定向优化方案（2026-08-27）

### 问题

前端请求 `/api/knowledge?repo=xxx`（不带尾斜杠），但 FastAPI 路由定义 `@router.get("/")`（带尾斜杠），导致 307 Temporary Redirect。dev 分支原有行为，不是 OBS 改造引入。

### 方案

只改路由 `"/"` → `""`，不改 `redirect_slashes=False`。

| 方案 | 不带斜杠 | 带斜杠 | 改前端？ | 风险 |
|------|:---:|:---:|:---:|:---:|
| 只改路由 `""`，保留 `redirect_slashes=True` | 直接 200 ✅ | 307→200（能工作）✅ | 不用改 ✅ | 低 |
| 改 `redirect_slashes=False` + 路由 `""` | 直接 200 ✅ | 404 ❌ | 必须改 | 高 |

选择只改路由——更安全，前端和测试都不用动。

### 改动清单（6 处，5 个文件）

| # | 文件 | 行号 | 当前 | 改为 |
|---|------|:---:|------|------|
| 1 | routes/knowledge.py | 249 | `@router.get("/")` | `@router.get("")` |
| 2 | routes/knowledge.py | 331 | `@router.post("/")` | `@router.post("")` |
| 3 | routes/knowledge.py | 420 | `@router.delete("/")` | `@router.delete("")` |
| 4 | routes/search.py | 9 | `@router.get("/")` | `@router.get("")` |
| 5 | routes/skills.py | 35 | `@router.get("/")` | `@router.get("")` |

不改：main.py `redirect_slashes`、前端 JS、测试代码。

### 影响分析

- 前端不带斜杠调用（knowledge / skills）：307→200 变为直接 200 ✅
- 前端带斜杠调用（search）：307→200 仍能工作 ✅
- 测试代码不带斜杠调用：307→200 变为直接 200 ✅
- 子路由（/skills/upload 等）：不受影响 ✅
- 后端内部调用：不受影响 ✅

## 二十五、knowledge_files 集群丢共享知识修复（2026-08-27）

### 问题

knowledge_files 调 read_knowledge 从 OBS 拿到 content 后，又 open 本地文件读取。集群 B 实例没有本地 knowledge.md → open 失败 → except 跳过 → 共享知识丢失。

### 修复

OBS 模式下直接用 read_knowledge 返回的 content，不 open 本地文件。
非 OBS 模式保持 open 读本地文件（和 dev 一致，保留原始 strip 行为）。

### 逻辑完备性检查

- agent_files OBS 分支 ✅（查 DB member+agent_type + OBS 读取）
- load_project_info OBS 分支 ✅（查 baseline + OBS 读取 + 兜底本地）
- knowledge_files OBS 分支 ✅（read_knowledge content + member_files OBS）
- generate_file_urls agent 部分 ✅（查 DB + 直接用 file_path）
- 所有 key 格式一致 ✅

## 二十六、第十六轮深度检查——集群 B 实例可用性（2026-08-27）

### 检查范围

逐个检查所有"OBS 优先 + 回退本地"的函数在集群 B 实例（无本地文件）的行为。

### 检查结果

| # | 函数 | OBS 分支 | 回退本地 | 集群 B 可用？ |
|---|------|:---:|:---:|:---:|
| 1 | knowledge_files | ✅ 用 content | 不需要 | ✅ |
| 2 | member_files | ✅ DB+OBS | 不需要 | ✅ |
| 3 | member_files_pi | ✅ DB+OBS | 不需要 | ✅ |
| 4 | agent_files | ✅ DB+OBS | 不需要 | ✅ |
| 5 | load_project_info | ✅ DB+OBS | 兜底本地 | ✅（DB 有就行） |
| 6 | vod_handler_async | ✅ DB+OBS | 兜底本地 | ⚠️（A 挂了才有问题） |
| 7 | _read_entry_content | ✅ OBS | 兜底本地 | ✅（返回空不报错） |
| 8 | _read_file | ✅ DB+OBS | 兜底本地 | ✅（obs_key 非空走 OBS） |

### 任务打包链路

enqueue_task → knowledge_files + member_files_pi + agent_files + load_project_info + generate_file_urls

全部走 OBS，不依赖本地磁盘 ✅

### vod_handler_async 集群限制

vod_handler_async 读基线文件逻辑：
1. 查 DB scope='baseline' + OBS 读取 → _baseline_content
2. OBS 没有 → 读本地文件 os.path.exists(_f) + open(_f)

集群 B 实例（无本地文件）+ OBS 读取失败时：
- _baseline_content 为空
- os.path.exists 返回 False
- 基线缺失 → VOD Agent 拿不到 project-graph.md / project-capabilities.md
- 不会报错 → _has_baseline 为 False 时直接跳过基线部分

实际不会发生：
1. 飞书 lease 隔离 → 只有 A 处理 webhook → VOD 任务只在 A 上执行
2. A 有本地文件 → OBS 失败也能兜底
3. 只有 A 挂了 + OBS 同时挂了才会出问题——概率极低

这是设计上的已知限制，不是 bug。

---

## 第十七轮：_member_docs + _agent_knowledge_entries 本地扫描残留修复

### 问题发现

集群部署后（A=本机 127.0.0.1 + B=242 127.0.0.1），项目知识页面数据不一致：

| 数据 | A（本机） | B（242） | 原因 |
|------|----------|----------|------|
| project files | 3 | 3 | OBS baseline ✅ 共享 |
| meta | 有 | 有 | OBS baseline ✅ 共享 |
| member_docs | 5 | 0 | **本地扫描 ❌** |
| agents | coding | 无 | **本地扫描 ❌** |

根因：`_member_docs()` 和 `_agent_knowledge_entries()` 的 agent 专属文档部分仍用 `os.listdir` / `os.path.isdir` 扫描本地 WORKSPACE_DIR，没有走 OBS。242 上没有本地 workspace 文件，所以返回空。

底层函数（member_files / agent_files / load_project_info / _read_entry_content）已有 OBS 分支，但这两个路由层函数没复用。

### 改动方案

#### 改动文件

仅 1 个文件：`src/api/routes/knowledge.py`

#### 改动 1：`_member_docs()` 重写（L137-170）

现状：
```python
def _member_docs(repo_key: str) -> list[dict]:
    docs = []
    for sub in ("member", "agent"):
        base = os.path.join(_repo_base(repo_key), "project-info", sub)
        if not os.path.isdir(base):
            continue
        for owner_dir in sorted(os.listdir(base)):  # 本地扫描
            ...
            for f in sorted(os.listdir(d)):  # 本地扫描
                docs.append({
                    "name": f,
                    "owner": owner_dir,
                    "path": rel,
                    "size": os.path.getsize(fp),  # 本地文件
                    "updated_at": _fmt_time(os.path.getmtime(fp)),  # 本地文件
                    "kind": sub,
                })
    return docs
```

改为：
```python
def _member_docs(repo_key: str) -> list[dict]:
    """查 DB scope='member' 记录构建文档清单（集群 OBS 模式不依赖本地磁盘）。

    OBS 模式：查 DB agent_shared_knowledge scope='member'，从 file_path 解析 owner/kind。
    本地模式（OBS 未启用）：回退 os.listdir 扫描本地目录（保持原行为）。
    """
    from src.session_manager import db, db_lock
    from src.storage import get_storage

    storage = get_storage()
    if storage is not None:
        # OBS 模式：查 DB 构建清单
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT id, file_path, agent_type, created_by, created_at, obs_key "
                "FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope='member' "
                "AND obs_key!='' AND obs_key IS NOT NULL "
                "ORDER BY id DESC",
                (repo_key,)
            )
            rows = c.fetchall()
        docs = []
        seen = set()  # file_path 去重（同文件多版本只展示最新）
        for rid, file_path, agent_type, created_by, created_at, obs_key in rows:
            if not file_path or file_path in seen:
                continue
            seen.add(file_path)
            # 从 file_path 解析 kind 和 owner
            # file_path 格式：project-info/member/{username}/{filename} 或 project-info/agent/{agent_type}/{filename}
            parts = file_path.replace("\\", "/").split("/")
            if len(parts) >= 4 and parts[0] == "project-info":
                kind = parts[1]  # "member" 或 "agent"
                owner = parts[2] if kind == "member" else (agent_type or parts[2])
                name = parts[-1]
            else:
                kind = "member"
                owner = created_by or ""
                name = os.path.basename(file_path)
            docs.append({
                "name": name,
                "owner": owner,
                "path": file_path,
                "size": 0,  # OBS 模式不查对象 size（列表展示不需要精确值）
                "updated_at": normalize_event_time(created_at or ""),
                "kind": kind,
            })
        return docs

    # 本地模式（OBS 未启用）：保持原有 os.listdir 逻辑
    docs = []
    for sub in ("member", "agent"):
        base = os.path.join(_repo_base(repo_key), "project-info", sub)
        if not os.path.isdir(base):
            continue
        try:
            for owner_dir in sorted(os.listdir(base)):
                d = os.path.join(base, owner_dir)
                if not os.path.isdir(d):
                    continue
                for f in sorted(os.listdir(d)):
                    fp = os.path.join(d, f)
                    if not os.path.isfile(fp):
                        continue
                    rel = os.path.relpath(fp, _repo_base(repo_key)).replace("\\", "/")
                    docs.append({
                        "name": f,
                        "owner": owner_dir,
                        "path": rel,
                        "size": os.path.getsize(fp),
                        "updated_at": _fmt_time(os.path.getmtime(fp)),
                        "kind": sub,
                    })
        except Exception as e:
            log.warning(f"member docs scan failed {repo_key} ({sub}): {e}")
    return docs
```

#### 改动 2：`_agent_knowledge_entries()` agent 专属文档部分（L237-249）

现状：
```python
    # Agent 专属成员上传文档（§六：project-info/agent/{agent_type}/，定向给该 Agent）
    from src.api.knowledge import agent_files
    import os as _os
    base = _os.path.join(_repo_base(repo_key), "project-info", "agent")
    if _os.path.isdir(base):  # 本地扫描
        for at in sorted(_os.listdir(base)):  # 本地扫描
            if not at or not _os.path.isdir(_os.path.join(base, at)):
                continue
            for rel, content in agent_files(repo_key, at).items():
                groups.setdefault(at, []).append({...})
```

改为：
```python
    # Agent 专属成员上传文档（§六：查 DB scope='member' AND agent_type!=''）
    # agent_files() 已有 OBS 分支，直接调用——不依赖本地磁盘
    from src.api.knowledge import agent_files
    from src.session_manager import db, db_lock
    # 查 DB 获取该 repo 下所有有 agent 专属文档的 agent_type 列表
    agent_types = set()
    try:
        with db_lock:
            c = db.cursor()
            c.execute(
                "SELECT DISTINCT agent_type FROM agent_shared_knowledge "
                "WHERE repo_key=? AND scope='member' AND agent_type!='' AND agent_type IS NOT NULL",
                (repo_key,)
            )
            agent_types = {r[0] for r in c.fetchall() if r[0]}
    except Exception:
        pass
    for at in sorted(agent_types):
        for rel, content in agent_files(repo_key, at).items():
            groups.setdefault(at, []).append({
                "scope": "member_agent", "agent_type": at, "issue_num": 0,
                "file_path": "", "path": "project-info/" + rel,
                "status": "accepted", "source_seq": 0,
                "created_by": "member", "created_at": "", "content": content,
            })
```

### 不改动的

- `src/api/knowledge.py` 的 member_files / agent_files / member_files_pi：已有 OBS 分支
- `load_project_info()`：已有 OBS 分支
- `_read_entry_content()`：已有 OBS 分支
- `_read_file()` / `_read_ws_file()`：已有 OBS 分支

### 兼容策略

- storage 为 None（OBS 未启用）→ `_member_docs()` 回退原有 os.listdir 逻辑
- DB 查不到 member 记录 → 返回空列表（与本地目录不存在时行为一致）
- agent_files() OBS 分支已处理 storage 为 None 的回退

### 验证方法

```bash
# 两台后端分别直连测试
TOKEN=***
curl -s "http://127.0.0.1:8000/api/knowledge?repo=xiaowu2119%2FBLOG-SYS-testV1" -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json; d=json.load(sys.stdin)
p=d.get('project',{})
print(f'files={len(p.get(\"files\",[]))} member_docs={len(p.get(\"member_docs\",[]))} agents={list(d.get(\"agents\",{}).keys())}')
"
curl -s "http://127.0.0.1:8000/api/knowledge?repo=xiaowu2119%2FBLOG-SYS-testV1" -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json; d=json.load(sys.stdin)
p=d.get('project',{})
print(f'files={len(p.get(\"files\",[]))} member_docs={len(p.get(\"member_docs\",[]))} agents={list(d.get(\"agents\",{}).keys())}')
"
# 预期：两台返回相同的 member_docs 数量和 agents 列表
```

### 深度分析补充——3 个遗漏问题

#### 问题 3：`_read_ws_file()` 没有 OBS 分支（L97-111）

现状：
```python
def _read_ws_file(repo_key: str, ws_rel: str) -> str:
    # 直接 open 本地文件，无 OBS 分支
    full = os.path.normpath(os.path.join(WORKSPACE_DIR, rel))
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, "文件不存在")
    with open(full, "r", encoding="utf-8") as f:
        return f.read()
```

`_read_file()` 有 OBS 分支（查 DB obs_key → OBS 读取），但 `_read_ws_file()` 没有。

`_read_ws_file()` 被 `_read_entry_content()` 调用作本地 fallback——OBS 读取失败时回退到 `_read_ws_file` 读本地。集群 B 实例（242）本地没有文件 → 404 → 返回空串。

虽然 OBS 读取成功时不会走到 `_read_ws_file`，但 OBS 读取失败时（网络抖动/对象被删）会 fallback 到本地，242 上就会失败。

改动 3：`_read_ws_file()` 加 OBS 分支

```python
def _read_ws_file(repo_key: str, ws_rel: str) -> str:
    """读 WORKSPACE_DIR 相对路径（防路径穿越）。

    OBS 模式：查 DB obs_key → OBS 读取（不依赖本地磁盘）。
    本地模式：open 本地文件（保持原行为）。
    """
    from src.config import WORKSPACE_DIR
    rel = _safe_rel(ws_rel)

    # OBS 分支：查 DB obs_key → OBS 读取
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT obs_key FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? "
                "ORDER BY id DESC LIMIT 1",
                (repo_key, rel)).fetchone()
        if row and row[0]:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                data = storage.read_bytes(row[0])
                if data is not None:
                    return data.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"OBS read failed for {repo_key}/{rel}: {e} — fallback to local")

    # 本地模式（OBS 未启用 / OBS miss）
    full = os.path.normpath(os.path.join(WORKSPACE_DIR, rel))
    if full != WORKSPACE_DIR and not full.startswith(WORKSPACE_DIR + os.sep):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法路径"})
    ext = os.path.splitext(rel)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"仅支持 {sorted(ALLOWED_EXTS)}"})
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"文件不存在: {rel}"})
    try:
        with open(full, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})
```

#### 问题 4：`delete_knowledge` 先查本地文件存在性（L438）

现状：
```python
if not os.path.isfile(full):
    raise HTTPException(status_code=404, "文件不存在")
# ... 后面才查 DB obs_key → 删 OBS
```

OBS 模式下请求打到 242，本地没有文件 → 直接 404，不会执行后面的 OBS 删除逻辑。用户在 242 上无法删除知识文档。

改动 4：OBS 模式下跳过本地文件存在性检查

```python
    # OBS 模式：先查 DB obs_key，有 obs_key 时不依赖本地文件存在
    obs_key = ""
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            row = db.cursor().execute(
                "SELECT obs_key FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? AND scope='member' "
                "ORDER BY id DESC LIMIT 1",
                (repo_key, rel)).fetchone()
        if row:
            obs_key = row[0] or ""
    except Exception:
        pass

    from src.storage import get_storage
    storage = get_storage()

    # 本地模式（OBS 未启用）：检查本地文件存在
    if storage is None:
        if not os.path.isfile(full):
            raise HTTPException(status_code=404, detail={"code": 404, "message": f"文件不存在: {rel}"})

    # 成员目录按用户名子目录校验本人（不变）
    if is_member:
        owner_part = rel.split("/")[2] if len(rel.split("/")) > 2 else ""
        me = str(user.get("username") or user.get("id") or "")
        if owner_part and owner_part != me:
            raise HTTPException(status_code=403, detail={"code": 403, "message": "仅可删除本人上传的文档"})

    # OBS 删除
    if obs_key and storage is not None:
        try:
            if not storage.delete_object(obs_key):
                raise HTTPException(status_code=500, detail={"code": 500, "message": "OBS 对象删除失败，已中止"})
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"OBS 删除失败: {e}"})

    # 本地文件删除（OBS 模式下本地不存在只 warning 不中止）
    try:
        os.remove(full)
    except Exception as e:
        if obs_key and storage is not None:
            log.warning(f"[knowledge] OBS 已删但本地文件删除失败: {full}: {e}")
        else:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"删除失败: {e}"})

    # 删 DB 记录（不变）
    try:
        from src.session_manager import db, db_lock
        with db_lock:
            c = db.cursor()
            c.execute("DELETE FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? AND scope='member'",
                      (repo_key, rel))
            db.commit()
    except Exception:
        pass
    return {"message": f"已删除 {os.path.basename(rel)}"}
```

关键变化：
- 查 DB obs_key 提前到本地文件检查之前
- storage 为 None（非 OBS）时才检查 os.path.isfile
- OBS 模式下本地文件删除失败只 warning 不中止（原有逻辑已如此，但被前面的 404 挡住了）

#### 问题 5：`upload_knowledge` 先写本地文件 + 重名检查只查本地（L368-374）

现状：
```python
os.makedirs(member_dir, exist_ok=True)
dest = os.path.join(member_dir, name)
if os.path.exists(dest):  # 只查本地
    dest = ...加时间戳
with open(dest, "w") as f:
    f.write(text)
```

两个问题：
1. OBS 模式下请求打到 242，在 242 本地创建文件，本机看不到——本地文件不同步
2. 重名检查 `os.path.exists(dest)` 只查本地，同一文件名在 242 和本机各上传一次会生成两个 DB 记录

改动 5：OBS 模式下重名检查改查 DB + 本地文件可选写

```python
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    base = _repo_base(repo_key)

    # 判断 OBS 模式
    from src.storage import get_storage
    storage = get_storage()

    if scope == "agent":
        member_dir = os.path.join(base, "project-info", "agent", agent_type)
    else:
        member_dir = os.path.join(base, "project-info", "member", str(user.get("username") or user.get("id") or "user"))

    # 重名检查：OBS 模式查 DB，本地模式查 os.path.exists
    actual_name = name
    if storage is not None:
        # OBS 模式：查 DB 是否已有同名文件
        try:
            from src.session_manager import db, db_lock
            with db_lock:
                existing = db.cursor().execute(
                    "SELECT 1 FROM agent_shared_knowledge WHERE repo_key=? AND file_path LIKE ? AND scope='member' LIMIT 1",
                    (repo_key, f"project-info/{'agent' if scope == 'agent' else 'member'}/%/{name}")).fetchone()
            if existing:
                stem, ext2 = os.path.splitext(name)
                actual_name = f"{stem}-{int(__import__('time').time())}{ext2}"
        except Exception:
            pass
    else:
        # 本地模式：查 os.path.exists
        os.makedirs(member_dir, exist_ok=True)
        dest = os.path.join(member_dir, name)
        if os.path.exists(dest):
            stem, ext2 = os.path.splitext(name)
            actual_name = f"{stem}-{int(__import__('time').time())}{ext2}"

    # 构建最终 rel path
    if scope == "agent":
        rel = f"project-info/agent/{agent_type}/{actual_name}"
    else:
        _username = str(user.get("username") or user.get("id") or "user")
        rel = f"project-info/member/{_username}/{actual_name}"

    # 写本地（OBS 模式下可选，写失败不报错）
    try:
        os.makedirs(member_dir, exist_ok=True)
        dest = os.path.join(member_dir, actual_name)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        if storage is None:
            raise HTTPException(status_code=500, detail={"code": 500, "message": f"写入失败: {e}"})
        log.warning(f"[knowledge] local write failed (OBS mode, non-fatal): {e}")

    # OBS 写入 + DB 索引（不变）
    obs_key = ""
    content_hash = ""
    if storage is not None:
        try:
            from src.api.knowledge import _owner_repo
            _owner, _rname = _owner_repo(repo_key)
            if scope == "agent":
                storage_key = f"knowledge/{_owner}/{_rname}/agent/{agent_type}/{actual_name}"
            else:
                _username = str(user.get("username") or user.get("id") or "user")
                storage_key = f"knowledge/{_owner}/{_rname}/member/{_username}/{actual_name}"
            obs_key = storage_key
            storage.save_bytes(obs_key, content.encode("utf-8") if isinstance(content, str) else content)
            import hashlib
            content_hash = hashlib.sha256(content.encode("utf-8") if isinstance(content, str) else content).hexdigest()
        except Exception as e:
            log.warning(f"[knowledge] OBS save failed for {repo_key}/{rel}: {e} — local file unaffected")
            obs_key = ""
            content_hash = ""
```

关键变化：
- OBS 模式重名检查改查 DB（LIKE 匹配 file_path 后缀）
- 本地文件写入改为可选（OBS 模式下写失败只 warning）
- rel path 构建不再依赖 os.path.relpath（改用字符串拼接，不依赖本地文件实际路径）

### 改动汇总（完整）

| # | 位置 | 改动内容 | OBS | 非OBS |
|---|------|---------|:---:|:-----:|
| 1 | `_member_docs()` L137 | os.listdir → 查 DB scope='member' | ✅ | 回退原逻辑 |
| 2 | `_agent_knowledge_entries()` L234 | os.listdir → 查 DB DISTINCT agent_type | ✅ | agent_files 回退本地 |
| 3 | `_read_ws_file()` L97 | 加 OBS 分支（查 DB obs_key → OBS 读取） | ✅ | 回退原逻辑 |
| 4 | `delete_knowledge()` L438 | OBS 模式跳过本地文件存在性检查 | ✅ | 保持原逻辑 |
| 5 | `upload_knowledge()` L368 | 重名检查改查 DB + 本地写入可选 | ✅ | 保持原逻辑 |

### 不改动的（已确认有 OBS 分支）

- `_read_file()`：已有 OBS 分支
- `_read_entry_content()`：已有 OBS 分支
- `member_files()` / `agent_files()` / `member_files_pi()`：已有 OBS 分支
- `load_project_info()`：已有 OBS 分支
- `_project_knowledge_entries()` / `_agent_knowledge_entries()` 的 issue/issue_agent 部分：通过 `_read_entry_content()` 走 OBS

### 验证方法（补充）

```bash
# 1. 列表一致性（改动 1+2）
curl -s "http://127.0.0.1:8000/api/knowledge?repo=xxx" -H "Authorization: Bearer $TOKEN"
curl -s "http://127.0.0.1:8000/api/knowledge?repo=xxx" -H "Authorization: Bearer $TOKEN"
# 预期：member_docs 数量和 agents 列表一致

# 2. 内容读取（改动 3）
curl -s "http://127.0.0.1:8000/api/knowledge/content?repo=xxx&file=project-info/member/xiaowu2119/blog-config.json" -H "Authorization: Bearer $TOKEN"
# 预期：返回文件内容（走 OBS），不是 404

# 3. 删除（改动 4）
curl -s -X DELETE "http://127.0.0.1:8000/api/knowledge?repo=xxx&file=project-info/member/xiaowu2119/test.md" -H "Authorization: Bearer $TOKEN"
# 预期：OBS 对象删除 + DB 记录删除，不因本地文件不存在而 404

# 4. 上传（改动 5）
curl -s -X POST "http://127.0.0.1:8000/api/knowledge" -H "Authorization: Bearer ***" -F "file=@test.md" -F "repo=xxx" -F "scope=project"
# 预期：OBS 写入 + DB 记录，本地文件可选写
```

### 第二轮深度分析——边界问题确认

对 routes/knowledge.py 全部函数逐个审查，确认以下边界情况：

#### 问题 6：write_knowledge 本地写入失败会阻断 OBS 写入（已知限制，不修复）

src/api/knowledge.py 的 `write_knowledge()` 逻辑：
1. 先写本地文件（`open(file_path, "a")` 追加）
2. 再读本地文件（`open(file_path, "rb")`）拿内容写 OBS
3. DB INSERT 带 obs_key + content_hash

如果请求打到 242，本地目录不存在或写入失败 → raise RuntimeError → 后面的 OBS 写入不执行。

**不修复原因**：
- `write_knowledge()` 被 `events.py add_knowledge()` 调用，走的是 webhook 流程
- 飞书 lease 机制保证同一 bot 同一时刻只有一个实例连接 → webhook 只在 lease 持有者（A=本机）上处理
- 本机有本地文件，不会写入失败
- 只有 A 挂了 B 接管 webhook 时才会触发，概率极低
- 即使触发，write_knowledge 抛 RuntimeError 会被 events.py 的 try/except 捕获，事件标记为 failed，不会丢数据

**风险评估**：低。集群故障转移场景下的知识写入可能失败，但不会导致数据不一致（写入失败 → 事件 failed → 用户重试）。

#### 问题 7：knowledge_context 端点调 _member_docs（自动修复，无需额外改动）

routes/knowledge.py L324：`knowledge_context` 端点调 `_member_docs(repo_key)` 生成成员文档摘要。

改动 1 修复了 `_member_docs()` 后，此处自动生效——OBS 模式下从 DB 查询，不依赖本地磁盘。无需额外改动。

#### 问题 8：search.py 的 @router.get("")（已修复，无需额外改动）

search.py 已经是 `@router.get("")`，不会触发 307 重定向。确认无误。

#### 问题 9：repos.py get_repo_project_info 调 load_project_info（已有 OBS 分支，无需改动）

`get_repo_project_info()` 调 `load_project_info()`，后者已有 OBS 分支（查 DB scope='baseline' + OBS 读取）。确认无误。

#### 问题 10：prompt_builder.py 调 member_files（已有 OBS 分支，无需改动）

`prompt_builder.py` L199/L795 调 `member_files()`，后者已有 OBS 分支（查 DB scope='member' + OBS 读取）。确认无误。

#### 问题 11：knowledge_files() 交付 worker（已有 OBS 分支，无需改动）

`knowledge_files()` 被 `enqueue_task` 调用，打包知识文件交付远端 worker。内部调 `read_knowledge()`（已有 OBS 分支）+ `member_files()`（已有 OBS 分支）。确认无误。

### 完整审查结论

对 routes/knowledge.py 全部 11 个函数/端点逐个审查：

| 函数/端点 | 依赖本地文件 | OBS 分支 | 需改动 |
|-----------|:----------:|:-------:|:-----:|
| `_read_file()` | 是 | ✅ 已有 | 否 |
| `_read_ws_file()` | 是 | ❌ 缺失 | **改动 3** |
| `_read_entry_content()` | 间接（调 _read_ws_file） | ✅ 已有（改动 3 后完全覆盖） | 否 |
| `_member_docs()` | 是 | ❌ 缺失 | **改动 1** |
| `_project_knowledge_entries()` | 间接（调 _read_entry_content） | ✅ 已有 | 否 |
| `_agent_knowledge_entries()` issue 部分 | 间接（调 _read_entry_content） | ✅ 已有 | 否 |
| `_agent_knowledge_entries()` agent 专属部分 | 是（os.listdir） | ❌ 缺失 | **改动 2** |
| `list_knowledge()` | 间接（调上述函数） | 改动后完全覆盖 | 否 |
| `read_knowledge_content()` | 间接（调 _read_file） | ✅ 已有 | 否 |
| `knowledge_context()` | 间接（调 _member_docs） | 改动后完全覆盖 | 否 |
| `upload_knowledge()` | 是（写本地 + 重名检查） | ❌ 缺失 | **改动 5** |
| `delete_knowledge()` | 是（检查本地存在 + 删本地） | ❌ 缺失 | **改动 4** |

对 src/api/knowledge.py 审查：

| 函数 | 依赖本地文件 | OBS 分支 | 需改动 |
|------|:----------:|:-------:|:-----:|
| `read_knowledge()` | 是（fallback） | ✅ 已有 | 否 |
| `write_knowledge()` | 是（先写本地再写 OBS） | ✅ 已有（已知限制问题 6） | 否 |
| `knowledge_files()` | 间接（调 read_knowledge + member_files） | ✅ 已有 | 否 |
| `member_files()` | 是 | ✅ 已有 | 否 |
| `member_files_pi()` | 是 | ✅ 已有 | 否 |
| `agent_files()` | 是 | ✅ 已有 | 否 |

对 src/runtime/service.py 审查：

| 函数 | 依赖本地文件 | OBS 分支 | 需改动 |
|------|:----------:|:-------:|:-----:|
| `load_project_info()` | 是（fallback） | ✅ 已有 | 否 |

对 src/project_info/builder.py 审查：

| 函数 | 依赖本地文件 | OBS 分支 | 需改动 |
|------|:----------:|:-------:|:-----:|
| `_write_file()` | 是（写本地 + 写 OBS） | ✅ 已有 | 否 |
| `_write_meta()` | 是（写本地 + 写 OBS） | ✅ 已有 | 否 |
| `_write_to_obs()` | 否（纯 OBS + DB） | ✅ 已有 | 否 |

对 src/prompt_builder.py 审查：

| 函数 | 依赖本地文件 | OBS 分支 | 需改动 |
|------|:----------:|:-------:|:-----:|
| `_build_project_info_section()` | 间接（调 load_project_info） | ✅ 已有 | 否 |
| `_build_knowledge_section()` | 间接（调 member_files + read_knowledge） | ✅ 已有 | 否 |

### 最终结论（第三轮分析后修订）

方案修订为 6 处改动。前 5 处覆盖 routes/knowledge.py 里缺失 OBS 分支的函数，改动 6 修复 _read_file 的路径格式不匹配问题（pre-existing bug，集群 B 实例上读取 project/issue 级知识会 404）。

其余文件（knowledge.py / service.py / builder.py / prompt_builder.py）的函数已有 OBS 分支，无需改动。

唯一已知限制是 `write_knowledge()` 的本地写入依赖（问题 6），但因 lease 机制保证 webhook 单实例处理，实际不会触发，不修复。

#### 问题 12：_read_file 的 DB 查询路径格式不匹配（pre-existing bug，需修复）

DB 里 file_path 有两种格式：
- upload_knowledge 写入的：`project-info/member/{user}/{file}`（相对 repo_base）
- write_knowledge / builder 写入的：`{owner}/{repo}/project-info/xxx`（相对 WORKSPACE_DIR）

前端通过 `_repo_root_rel()` 把 DB 的 file_path 剥掉 owner/repo 前缀后作为 path 返回给前端。用户点击后前端把这个 path 作为 file 参数传给 content 端点。

`_read_file()` 用这个 stripped path 查 DB：
- member 文档：`project-info/member/xxx` → DB 里也是这个格式 → 匹配 ✅
- project/issue 知识：`project-info/knowledge.md`（stripped）→ DB 里是 `owner/repo/project-info/knowledge.md` → 不匹配 ❌ → OBS 读取失败 → 回退本地文件 → 集群 B 实例（242）上 404

实测确认：
```
curl "http://127.0.0.1:8000/api/knowledge/content?repo=xiaowu2119%2FBLOG-SYS-testV1&file=project-info/knowledge.md"
→ 404 "文件不存在: project-info/knowledge.md"
```

改动 6：_read_file 的 OBS 查询增加二次匹配

```python
def _read_file(repo_key: str, rel: str) -> str:
    rel = _safe_rel(rel)
    ext = os.path.splitext(rel)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail={"code": 400, "message": f"仅支持 {sorted(ALLOWED_EXTS)}"})

    # OBS 分支：查 DB obs_key，非空走 storage.read_bytes
    from src.session_manager import db, db_lock
    try:
        with db_lock:
            # 第一次：用 rel 原样查（匹配 upload_knowledge 格式）
            row = db.cursor().execute(
                "SELECT obs_key FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? "
                "ORDER BY id DESC LIMIT 1",
                (repo_key, rel)).fetchone()
            # 第二次：用 {owner}/{repo}/{rel} 查（匹配 write_knowledge / baseline 格式）
            if not row or not row[0]:
                from src.repo_key import split_owner_repo
                _owner, _rname = _owner_repo(repo_key)
                if _owner and _rname:
                    full_rel = f"{_owner}/{_rname}/{rel}"
                    row = db.cursor().execute(
                        "SELECT obs_key FROM agent_shared_knowledge WHERE repo_key=? AND file_path=? "
                        "ORDER BY id DESC LIMIT 1",
                        (repo_key, full_rel)).fetchone()
        if row and row[0]:
            from src.storage import get_storage
            storage = get_storage()
            if storage is not None:
                data = storage.read_bytes(row[0])
                if data is not None:
                    return data.decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"OBS read failed for {repo_key}/{rel}: {e} — fallback to local")

    # 本地模式（storage 为 None 或 OBS miss）— 保持原逻辑不变
    base = _repo_base(repo_key)
    full = os.path.normpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail={"code": 400, "message": "非法路径"})
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail={"code": 404, "message": f"文件不存在: {rel}"})
    try:
        with open(full, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail={"code": 500, "message": f"读取失败: {e}"})
```

关键变化：
- OBS 查询先用 rel 原样查（匹配 upload_knowledge 的 `project-info/member/xxx` 格式）
- 查不到再用 `{owner}/{repo}/{rel}` 查（匹配 write_knowledge 的 `owner/repo/project-info/xxx` 格式）
- 两次查询覆盖 DB 里两种 file_path 格式

#### 同样影响 _read_ws_file（改动 3 补充）

`_read_ws_file` 的 OBS 分支也有同样问题。但 `_read_ws_file` 接收的 `ws_rel` 直接来自 DB 的 file_path 字段（通过 `_read_entry_content`），所以：
- write_knowledge 条目：ws_rel = `owner/repo/project-info/xxx` → 查 DB 匹配 ✅
- upload_knowledge 条目：ws_rel = `project-info/member/xxx` → 查 DB 匹配 ✅

`_read_ws_file` 不会收到 stripped path（它不走前端），所以改动 3 的 OBS 查询不需要二次匹配。确认无误。

### 改动 6 方案优化——直接用 obs_key 替代反查 DB

#### 原方案问题

改动 6 原方案是在 `_read_file()` 里做两次 DB 查询（先用 rel 查，再用 owner/repo/rel 查），绕了一圈来处理路径格式不匹配。

但 DB 里已经存了 obs_key，列表接口也返回了 obs_key——完全不需要前端传 path 再反查 DB。

#### 优化方案

前端传 obs_key 给 content 端点，后端直接用 obs_key 读 OBS，彻底消除路径格式问题。

涉及两个文件：

##### 1. 前端 knowledge.js

列表接口已经返回每个条目的 obs_key（issue/issue_agent/project 条目通过 `_project_knowledge_entries` / `_agent_knowledge_entries` 返回 obs_key 字段）。

但 `_member_docs()`（改动 1）返回的条目目前没有 obs_key 字段——需要在改动 1 里补上。

前端 `loadKnowledgeContent()` 改为传 obs_key：

```javascript
// 现状：传 path
onclick="loadKnowledgeContent('${k.path || k.file_path}')"

// 改为：传 obs_key（有 obs_key 时优先），无 obs_key 时回退 path
onclick="loadKnowledgeContent('${k.obs_key || k.path || k.file_path}')"
```

```javascript
// loadKnowledgeContent 函数改造
function loadKnowledgeContent(key) {
  // 如果 key 以非 / 开头且不是 project-info/ 开头，视为 obs_key
  const params = key && !key.startsWith('project-info/') && !key.startsWith('/')
    ? `obs_key=${encodeURIComponent(key)}`
    : `file=${encodeURIComponent(key)}`;
  API.get('/knowledge/content?repo=' + encodeURIComponent(_currentRepo()) + '&' + params).then(d => {
    // ... 渲染逻辑不变
  });
}
```

##### 2. 后端 read_knowledge_content 端点 + _read_file

content 端点新增 `obs_key` 查询参数，有 obs_key 时直接读 OBS，不走 _read_file 反查：

```python
@router.get("/content")
async def read_knowledge_content(repo: str = Query(""),
                                 file: str = Query(""),
                                 obs_key: str = Query(""),
                                 user: dict = Depends(get_current_user)):
    """读取单个知识全文（相对工作区仓库目录）或通过 obs_key 直接读 OBS。"""
    if not repo:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "必须指定 repo"})
    if not file and not obs_key:
        raise HTTPException(status_code=400, detail={"code": 400, "message": "file 或 obs_key 必填"})

    # obs_key 模式：直接读 OBS，不需要反查 DB
    if obs_key:
        from src.storage import get_storage
        storage = get_storage()
        if storage is not None:
            try:
                data = storage.read_bytes(obs_key)
                if data is not None:
                    from src.repo_key import to_repo_key
                    return {"repo_key": to_repo_key(repo), "obs_key": obs_key,
                            "content": data.decode("utf-8", errors="replace")}
            except Exception as e:
                log.warning(f"OBS read failed for obs_key={obs_key}: {e} — fallback to file")
        # OBS miss → 回退 file 参数走 _read_file

    # file 模式（原有逻辑，兼容非 OBS / OBS miss / 前端未传 obs_key）
    from src.repo_key import to_repo_key
    repo_key = to_repo_key(repo)
    return {"repo_key": repo_key, "file": file, "content": _read_file(repo_key, file)}
```

##### 3. 改动 1 _member_docs 补充 obs_key 字段

改动 1 的 `_member_docs()` OBS 分支返回的条目需要加上 obs_key：

```python
docs.append({
    "name": name,
    "owner": owner,
    "path": file_path,
    "obs_key": obs_key,  # 新增——供前端 content 端点直接用
    "size": 0,
    "updated_at": normalize_event_time(created_at or ""),
    "kind": kind,
})
```

##### 优化方案优势

| 对比项 | 原方案（改动 6 二次匹配） | 优化方案（obs_key 直传） |
|--------|-------------------------|------------------------|
| DB 查询次数 | 2 次（rel + owner/repo/rel） | 0 次（直接用 obs_key） |
| 路径格式依赖 | 依赖两种格式都能匹配 | 完全不依赖路径格式 |
| 代码复杂度 | _read_file 逻辑变复杂 | _read_file 不改，新增 obs_key 分支 |
| 兼容性 | 只改后端 | 前后端都改，但兼容旧前端（file 参数仍可用） |

##### 兼容策略

- 前端传 obs_key → 后端走 OBS 直读
- 前端传 file（旧前端 / obs_key 为空） → 后端走 _read_file（原有逻辑）
- _read_file 保持原有代码不变，不改动 6 的二次匹配
- 非 OBS 模式：obs_key 为空 → 前端回退传 file → 走本地文件

### 改动汇总（最终，5 处后端 + 1 处前端）

| # | 位置 | 改动内容 | OBS | 非OBS |
|---|------|---------|:---:|:-----:|
| 1 | `_member_docs()` L137 | os.listdir → 查 DB scope='member' + 返回 obs_key 字段 | ✅ | 回退原逻辑 |
| 2 | `_agent_knowledge_entries()` L234 | os.listdir → 查 DB DISTINCT agent_type | ✅ | agent_files 回退本地 |
| 3 | `_read_ws_file()` L97 | 加 OBS 分支（查 DB obs_key → OBS 读取） | ✅ | 回退原逻辑 |
| 4 | `delete_knowledge()` L438 | OBS 模式跳过本地文件存在性检查 | ✅ | 保持原逻辑 |
| 5 | `upload_knowledge()` L368 | 重名检查改查 DB + 本地写入可选 | ✅ | 保持原逻辑 |
| 6 | `read_knowledge_content()` + 前端 | content 端点新增 obs_key 参数，前端传 obs_key 直读 OBS | ✅ | 回退 file 参数 |

注：改动 6 原方案（_read_file 二次匹配）废弃，改为 obs_key 直传方案。_read_file 保持原代码不变。

### 兼容性确认（最终）

每个改动都有 `storage is not None` 分支判断，storage 为空时回退原有本地文件逻辑。

| 改动 | OBS 模式 | 非OBS 模式 | 旧前端访问新后端 |
|------|---------|-----------|----------------|
| 1 _member_docs | 查 DB + 返回 obs_key | 回退 os.listdir，条目无 obs_key | N/A |
| 2 _agent_knowledge_entries | 查 DB DISTINCT agent_type | DB 有记录（obs_key 空）+ agent_files 走本地 | N/A |
| 3 _read_ws_file | 查 DB obs_key → OBS 读取 | storage 为 None → 跳过 → open 本地 | N/A |
| 4 delete_knowledge | 跳过本地检查 → 删 OBS → 本地删失败 warning | 检查 os.path.isfile → 删本地 → 删 DB | N/A |
| 5 upload_knowledge | 重名查 DB → 本地可选写 → 写 OBS + DB | 重名查 os.path.exists → 写本地 → DB obs_key 空 | N/A |
| 6 content 端点 + 前端 | 前端传 obs_key → 直读 OBS | obs_key 空 → 前端回退传 file → 走 _read_file | 只传 file → obs_key 空 → 走 _read_file |

结论：6 处改动全部两种模式都支持且向前兼容。

注：改动 6 原方案（_read_file 二次匹配）废弃，改为 obs_key 直传方案。_read_file 保持原代码不变。

### 实施后发现并修复的 2 个遗漏

#### 遗漏 1：project_files 没有 obs_key 字段

list_knowledge 构造 project_files 时只有 name/label/updated，没有 obs_key。前端点击 project-graph.md → 传 "project-info/project-graph.md" → _read_file 查 DB WHERE file_path = "project-info/project-graph.md" → DB 里是 "xiaowu2119/BLOG-SYS-testV1/project-info/project-graph.md" → 不匹配 → 回退本地 → 242 上 404。

修复：list_knowledge 里查 DB scope='baseline' 的 obs_key，按 filename 映射到 project_files 条目：

```python
# 查 DB baseline obs_key
baseline_obs = {}
rows = db.cursor().execute(
    "SELECT file_path, obs_key FROM agent_shared_knowledge "
    "WHERE repo_key=? AND scope='baseline' AND obs_key!='' AND obs_key IS NOT NULL",
    (repo_key,)
).fetchall()
for fp, ok in rows:
    baseline_obs[os.path.basename(fp)] = ok

# project_files 条目加上 obs_key
{"name": "project-info/project-graph.md", "label": "Project Graph",
 "updated": ..., "obs_key": baseline_obs.get("project-graph.md", "")}
```

#### 遗漏 2：member_agent entries 没有 obs_key 字段

_agent_knowledge_entries 的 member_agent 部分构造条目时没有 obs_key。前端 `k.obs_key || k.path` 取 path → 走 _read_file → 虽然 DB 能匹配（upload_knowledge 格式），但多了一次 DB 查询，没走 obs_key 直传路径。

修复：查 DB 时一并获取 obs_key，构建 agent_type → {file_path: obs_key} 映射：

```python
# 查 DB 获取 member+agent_type 条目（含 obs_key）
c.execute(
    "SELECT agent_type, file_path, obs_key, created_by, created_at "
    "FROM agent_shared_knowledge "
    "WHERE repo_key=? AND scope='member' AND agent_type!='' "
    "AND obs_key!='' AND obs_key IS NOT NULL ORDER BY id DESC",
    (repo_key,)
)
# 构建 agent_obs_map: {agent_type: {file_path: obs_key}}
# 每个 member_agent 条目加上 obs_key
groups.setdefault(at, []).append({
    ..., "obs_key": agent_obs_map.get(at, {}).get(full_path, ""),
})
```

#### 修复后验证

所有条目都有 obs_key：
```
project files: project-graph.md obs_key=YES ✅
               project-capabilities.md obs_key=YES ✅
               .metadata.json obs_key=YES ✅
member_docs:   5 个全部 obs_key=YES ✅
agents:        coding/member_agent obs_key=YES ✅
```

obs_key 直读 OBS 验证通过：
```
curl "http://127.0.0.1:8000/api/knowledge/content?repo=xxx&obs_key=project-info/gitcode:xiaowu2119/BLOG-SYS-testV1/project-graph.md"
→ 200, 返回完整 project-graph.md 内容
```

### 最终改动汇总（10 处，含实施后补充）

| # | 位置 | 改动内容 | OBS | 非OBS |
|---|------|---------|:---:|:-----:|
| 1 | `_member_docs()` L137 | os.listdir → 查 DB scope='member' + 返回 obs_key 字段 | ✅ | 回退原逻辑 |
| 2 | `_agent_knowledge_entries()` L234 | os.listdir → 查 DB member+agent_type + obs_key 映射 | ✅ | agent_files 回退本地 |
| 3 | `_read_ws_file()` L97 | 加 OBS 分支（查 DB obs_key → OBS 读取） | ✅ | 回退原逻辑 |
| 4 | `delete_knowledge()` L438 | OBS 模式跳过本地文件存在性检查 | ✅ | 保持原逻辑 |
| 5 | `upload_knowledge()` L368 | 重名检查改查 DB + 本地写入可选 | ✅ | 保持原逻辑 |
| 6 | `read_knowledge_content()` + 后端 | content 端点新增 obs_key 参数，直接读 OBS | ✅ | 回退 file 参数 |
| 7 | `list_knowledge()` L347 | project_files 补 obs_key（查 DB baseline） | ✅ | obs_key 为空，前端回退 file |
| 8 | `_agent_knowledge_entries()` L322 | member_agent entries 补 obs_key（从 DB 查询映射） | ✅ | obs_key 为空，前端回退 path |
| 9 | 前端 knowledge.js onclick | 所有 loadKnowledgeContent 调用改为 `obs_key \|\| path \|\| name` | ✅ | obs_key 为空时回退 path |
| 10 | 前端 renderKnowledgeDetail | 显示标题/JSON 判断改为 `d.file \|\| d.obs_key` | ✅ | obs_key 模式下不显示 undefined |

### 最终验证结果（全数据流）

所有 4 种数据流在 OBS 模式下验证通过（不依赖本地磁盘）：

| 数据流 | 列表查询 | 内容读取 | 验证结果 |
|--------|---------|---------|---------|
| project-graph.md / capabilities / metadata | load_project_info (OBS baseline) + 补 obs_key (改动7) | obs_key 直读 OBS (改动6) | 200, 2805 bytes ✅ |
| member_docs (member 上传) | _member_docs 查 DB (改动1) | obs_key 直读 OBS (改动6) | 200, 230 bytes ✅ |
| member_agent (agent 专属) | _agent_knowledge_entries 查 DB (改动2+8) | obs_key 直读 OBS (改动6) | 200, 236 bytes ✅ |
| knowledge_context 上下文 | _member_docs + prompt_builder (全 OBS) | 不走 content 端点 | 200, 5254 bytes ✅ |

所有条目都有 obs_key：
- project files: 3 个全部 obs_key=YES ✅
- member_docs: 5 个全部 obs_key=YES ✅
- agents: 2 个全部 obs_key=YES ✅

### 代码审计确认

routes/knowledge.py 中所有 os.path 文件系统调用都在本地回退分支里（storage is None 或 OBS miss 时执行），OBS 分支只用 os.path.basename/splitext 做字符串解析，不访问文件系统。

8 个 pytest 基线失败（test_api_routes + test_runtime_agent）是改动前就存在的，与 knowledge 改动无关（git stash 对照确认）。knowledge 相关功能全部验证通过。
