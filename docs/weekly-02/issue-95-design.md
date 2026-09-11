# Issue #95: 项目知识注入 Agent 逻辑优化设计

## 1. 问题总结

Issue #67 只把 `knowledge_files()` 和 `_build_knowledge_section()` 两条路径接了 ref 表。其余 5 条路径仍全量注入，导致未关联知识（含 credential 文件）通过内容和 URL 两条通道泄漏。

### 问题清单

| # | 问题 | 现状 | 影响 |
|---|------|------|------|
| 1 | knowledge_files() 缺 _is_credential_file 过滤 | 走 ref 表但没加 credential 过滤 | credential 文件明文打包给 worker |
| 2 | member_files_pi() 没接 ref 表 | 全量查 scope=member，3 处调用 | 未关联知识全量注入 project_info_files |
| 3 | agent_files() 没接 ref 表 | 全量查 scope=member+agent_type，2 处调用 | 未关联知识全量注入 project_info_files |
| 4 | generate_file_urls() 没接 ref 表 | 全量查 member 生成 OBS URL | 未关联知识 URL 暴露给 worker |
| 5 | ~~_build_knowledge_section 只在 chat 路径调用~~ | build_devops_agent_prompt 不调它 | **不改**——devops 路径靠 worker 物化 knowledge_files 交付知识，不需要 prompt 内联 |
| 6 | vod_handler_async 独立注入 baseline | 直接查 scope=baseline，第三套注入逻辑 | 三套注入逻辑不统一 |
| 7 | _is_credential_file 缺 "cr" 关键词 | _CREDENTIAL_SUBSTRINGS 无 "cr" | repo-cr.json 未被过滤 |
| 8 | baseline OBS key 格式不统一 | project-info/{repo_key}/{filename} | 和其他 scope key 前缀不一致 |

## 2. 设计原则

**统一入口**：所有知识注入路径（content + URL + prompt 内联）都走 ref 表查询，关联了才注入。

**三维匹配**：scene_id + agent_type + issue_num，和 `_build_knowledge_section` / `knowledge_files` 的 SQL 完全一致。

**credential 过滤**：所有路径在拿到知识文件后统一过 `_is_credential_file`。

## 3. 改动方案

### 3.1 抽取统一查询函数（新增）

在 `src/api/knowledge.py` 新增 `query_knowledge_refs()`：

```python
def query_knowledge_refs(repo_key, agent_type="", issue_num=0, scene_id="") -> list[dict]:
    """查 ref 表返回关联知识列表（含 obs_key/file_path/content_hash/source/scope）。
    统一入口：knowledge_files / member_files_pi / agent_files / generate_file_urls 都调此函数。
    """
    # 同 _build_knowledge_section 的 SQL
    # 返回 [{id, scope, source, obs_key, file_path, content_hash, agent_type, scene_id}, ...]
```

### 3.2 knowledge_files() 加 credential 过滤（问题 1）

```python
for kid, obs_key, file_path, content_hash in rows:
    if _is_credential_file(file_path):  # ← 新增
        continue
    content = read_entry_content(...)
    if content:
        files[file_path or ""] = content
```

### 3.3 member_files_pi() 接 ref 表（问题 2）

改为调 `query_knowledge_refs()`，只返回关联的 member 知识：

```python
def member_files_pi(repo_key, agent_type="", issue_num=0, scene_id="") -> dict:
    rows = query_knowledge_refs(repo_key, agent_type, issue_num, scene_id)
    files = {}
    for r in rows:
        if r["scope"] != "member" or r.get("agent_type"):
            continue  # member_files_pi 只管全局 member（agent_type=''）
        if _is_credential_file(r["file_path"]):
            continue
        content = read_entry_content(...)
        if content:
            files[rel] = content
    return files
```

**调用方改签名**（加 agent_type/issue_num/scene_id 参数）：
- `service.py:419` — `member_files_pi(repo_key, agent_type, issue_num, scene_id)`
- `event_pipeline.py:1663` — 同上

### 3.4 agent_files() 接 ref 表（问题 3）

改为调 `query_knowledge_refs()`，只返回关联的 agent 专属 member 知识：

```python
def agent_files(repo_key, agent_type="", issue_num=0, scene_id="") -> dict:
    rows = query_knowledge_refs(repo_key, agent_type, issue_num, scene_id)
    files = {}
    for r in rows:
        if r["scope"] != "member" or r.get("agent_type") != agent_type:
            continue  # agent_files 只管 agent_type 匹配的 member
        if _is_credential_file(r["file_path"]):
            continue
        content = read_entry_content(...)
        if content:
            files[rel] = content
    return files
```

**调用方改签名**：
- `service.py:427` — `agent_files(repo_key, agent_type, issue_num, scene_id)`
- `event_pipeline.py:1665` — 同上

### 3.5 generate_file_urls() 接 ref 表（问题 4）

改为调 `query_knowledge_refs()`，只给关联知识生成 URL：

```python
def generate_file_urls(self, repo_key, agent_type="", issue_num=0, scene_id="") -> dict:
    from src.api.knowledge import query_knowledge_refs
    rows = query_knowledge_refs(repo_key, agent_type, issue_num, scene_id)
    out = {}
    for r in rows:
        if _is_credential_file(r["file_path"]):
            continue
        if r["obs_key"]:
            out[rel] = {"url": self.temporary_url(r["obs_key"]), "storage_key": r["obs_key"]}
    return out
```

**调用方改签名**：
- `service.py:455` — `_storage.generate_file_urls(repo_key, agent_type, _inum, scene_id)`

### 3.6 ~~build_devops_agent_prompt 接入 _build_knowledge_section~~（问题 5 — 不改）

devops 路径（build_devops_agent_prompt）走 worker 队列模式，知识通过 `knowledge_files` 物化到 `.knowledge/` 目录，Agent 用 read tool 读取。不需要 prompt 内联——`_build_knowledge_section` 内联是 chat 路径专用（chat 没有 worker 物化）。

只要 `knowledge_files` 走 ref 表 + credential 过滤（问题 1），devops 路径就正常。

### 3.7 vod_handler_async 统一注入（问题 6）

`_build_vod_prompt` 的 baseline 注入逻辑改为调 `_build_project_info_section`：

```python
# 现有：直接查 DB scope=baseline + OBS 读取 + 内联
# 改为：
parts.append(_build_project_info_section(repo_key, agent_type="vod"))
```

删除 vod_handler_async 里第 205-250 行的独立 baseline 查询逻辑。

### 3.8 _is_credential_file 关键词加 "cr"（问题 7）

```python
_CREDENTIAL_SUBSTRINGS = (
    "credential", "token", "secret", "password", "passwd",
    "apikey", "api_key", "api-key",
    "private_key", "private-key",
    "access_key", "access-key",
    "cr",  # ← 新增：覆盖 repo-cr.json 等变体
)
```

**注意**："cr" 是短词，可能误匹配文件名含 "cr" 的普通文件（如 "crawler.md"）。需要确认——是匹配 basename 子串还是精确匹配文件名前缀？

建议改为 `"repo-cr"` 而非 `"cr"`——精确匹配 `repo-cr.json` 这类凭证文件，不误匹配。

### 3.9 baseline OBS key 统一格式（问题 8）

**当前**：`project-info/{repo_key}/{filename}`
**目标**：`knowledge/{owner}/{repo}/project-info/{filename}`

改动点：
- `src/project_info/builder.py` 第 132 行 `_write_baseline_record` — baseline 生成的源头，改为新格式
- `src/api/knowledge.py` 第 769 行 sync_loop — 同步时用新格式
- `src/api/knowledge.py` 第 861 行 _sync_baseline_files — 补扫时用新格式
- `src/storage/obs.py` generate_file_urls — baseline URL 生成用新 key

**老数据不需要手动迁移**：

1. 老数据 DB 里 obs_key 是旧格式 `project-info/{repo_key}/{filename}`，OBS 对象也在旧 key 下。`storage.read_bytes(旧key)` 通过 `_full()` 自动拼前缀能正常读取——功能不受影响。

2. baseline 重新生成时（codegraph 分析 / 仓库代码变更），`builder.py` 的 `_write_baseline_record` 用 `INSERT OR REPLACE` 重写 DB 记录 + obs_key——自动用新格式写入 OBS + 更新 DB。

3. `_sync_baseline_files` 只处理 obs_key 为空的记录（第 854 行 `if row and row[1]: continue`），不会重写已有 obs_key 的老数据。但 baseline 重新生成会走 `builder.py` 重写——这是自动迁移的触发点。

4. 两种格式并存期间不影响读取——`read_entry_content` 用 DB 里的 obs_key 直接读 OBS，不管 key 格式是什么。

## 4. 改动文件

| 文件 | 改动 |
|------|------|
| `src/api/knowledge.py` | 新增 query_knowledge_refs()；knowledge_files 加 credential 过滤；member_files_pi/agent_files 接 ref 表；baseline OBS key 统一；_is_credential_file 加 "repo-cr" |
| `src/project_info/builder.py` | _write_baseline_record baseline OBS key 改为新格式 |
| `src/storage/obs.py` | generate_file_urls 接 ref 表 + credential 过滤 |
| `src/im/vod_handler_async.py` | _build_vod_prompt 改调 _build_project_info_section |
| `src/runtime/service.py` | member_files_pi/agent_files/generate_file_urls 调用方加参数 |
| `src/api/event_pipeline.py` | member_files_pi/agent_files 调用方加参数 |
| `tests/test_knowledge_ref.py` | 新增 credential 过滤测试 |
| `tests/test_knowledge_board.py` | 更新 member_files_pi/agent_files 测试 |
| `docs/weekly-02/issue-95-design.md` | 本设计文档 |

## 5. 不改的点

- `_build_project_info_section` 保留——baseline 摘要内联 + 文件路径指引，和 `_build_knowledge_section` 不冲突
- `load_project_info` 保留——加载本地 baseline 文件，给 `_build_project_info_section` 用
- 401 被动刷新 / token 主动刷新 不受影响
- ref 表结构不变

## 6. 风险

- **member_files_pi/agent_files 签名变化**——加了 3 个参数，所有调用方都要改
- **generate_file_urls 签名变化**——加了 scene_id 参数
- **build_devops_agent_prompt**——不改，无 prompt 变长风险
- **baseline OBS key**——老数据不需要手动迁移，`_sync_baseline_files` 自动逐步迁移
- **"repo-cr" 关键词**——需要确认不会误匹配正常文件

## 7. 验收标准

- [ ] knowledge_files() 不含 credential 文件
- [ ] member_files_pi() 接 ref 表，只返回关联知识
- [ ] agent_files() 接 ref 表，只返回关联知识
- [ ] generate_file_urls() 接 ref 表，只给关联知识生成 URL
- [x] ~~build_devops_agent_prompt 注入 ref 表知识~~ — 不改，devops 靠 worker 物化
- [ ] vod_handler_async 用统一注入逻辑
- [ ] _is_credential_file 包含 "repo-cr"
- [ ] baseline OBS key 统一为 knowledge/{owner}/{repo}/project-info/{filename}
- [ ] 现有测试通过，新增 credential 过滤测试
