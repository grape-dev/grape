# 设计文档：Issue #358 — 知识库知识大小限制 10KB，用户反映太小

> 基线：weekly-07-clean (d40219f22) | 分支：feat/issue-358-knowledge-content-limit | P1

## 一、问题分析

### 现状
知识库单条知识正文大小限制硬编码为 10KB，分布在 3 处后端 + 4 处前端 + 4 条 i18n 文案：

| # | 文件 | 行号 | 硬编码值 |
|---|------|------|----------|
| 后端1 | src/api/routes/knowledge.py:229 | MAX_CONTENT_BYTES = 10 * 1024 | 10KB |
| 后端2 | src/api/routes/knowledge.py:402-403 | 创建路径校验+报错 | "10KB" |
| 后端3 | src/api/routes/knowledge.py:595-596 | 更新路径校验+报错 | "10KB" |
| 前端1 | knowledge.js:449 | importFile: file.size > 10240 | 10KB |
| 前端2 | knowledge.js:489 | saveEdit: Blob.size > 10240 | 10KB |
| 前端3 | knowledge.js:640 | saveCreate: Blob.size > 10240 | 10KB |
| 前端4 | knowledge.js:677 | isFileTooLarge: content.length > 10240 | 10KB |
| i18n1 | zh/knowledge.json:82 | file_too_large | "文件超过 10KB 限制" |
| i18n2 | zh/knowledge.json:118 | content_too_large | "内容不能超过 10KB" |
| i18n3 | en/knowledge.json:82 | file_too_large | "File exceeds 10KB limit" |
| i18n4 | en/knowledge.json:118 | content_too_large | "Content cannot exceed 10KB" |

### 遗漏路径（PR #502 未覆盖）
1. **前端 4 处硬编码 10240 未改**：用户在前端创建/编辑知识时，JS 直接拦截超过 10KB 的内容，请求不会发到后端
2. **i18n 文案 4 处未改**：用户看到的提示仍然是"10KB"
3. **agent 写入路径无校验**：POST /api/events/{seq}/knowledge → write_knowledge() 完全没有大小限制
4. **知识注入 prompt 无截断**：_build_agent_knowledge_section() 读取知识后直接拼入 prompt，100KB 知识 ≈ 25K~50K tokens，可能超出 LLM context window

## 二、方案设计

### 2.1 后端常量可配置化

将 `MAX_CONTENT_BYTES` 从硬编码改为环境变量 `KNOWLEDGE_MAX_CONTENT_BYTES` 读取，默认 100KB。

```python
# src/api/routes/knowledge.py

_DEFAULT_MAX_CONTENT_BYTES = 100 * 1024
_MIN_CONTENT_BYTES = 1024  # 下限 1KB

def _resolve_max_content_bytes() -> int:
    raw = os.environ.get("KNOWLEDGE_MAX_CONTENT_BYTES")
    if raw is None or raw == "":
        return _DEFAULT_MAX_CONTENT_BYTES
    try:
        value = int(raw)
    except ValueError:
        log.warning(f"[knowledge] KNOWLEDGE_MAX_CONTENT_BYTES={raw!r} 不是合法整数，回退默认值 {_DEFAULT_MAX_CONTENT_BYTES}")
        return _DEFAULT_MAX_CONTENT_BYTES
    if value < _MIN_CONTENT_BYTES:
        log.warning(f"[knowledge] KNOWLEDGE_MAX_CONTENT_BYTES={value} 低于下限 {_MIN_CONTENT_BYTES}，已钳制为 {_MIN_CONTENT_BYTES}")
        return _MIN_CONTENT_BYTES
    return value

MAX_CONTENT_BYTES = _resolve_max_content_bytes()

def _format_content_limit(limit_bytes: int) -> str:
    if limit_bytes >= 1024 * 1024 and limit_bytes % (1024 * 1024) == 0:
        return f"{limit_bytes // (1024 * 1024)}MB"
    return f"{max(1, limit_bytes // 1024)}KB"
```

创建/更新两处报错改为 `f"content 不能超过 {_format_content_limit(MAX_CONTENT_BYTES)}"`。

### 2.2 stats 接口附带 limit（前端动态获取）

前端已有 `loadStats()` 在 mounted 时调用。在 stats 响应中附加 `content_limit_bytes`，前端无需新增 API 调用。

```python
# knowledge_stats() 返回增加：
return {
    "system": ..., "agent": ..., "manual": ..., "unlinked": ...,
    "content_limit_bytes": MAX_CONTENT_BYTES,
    "content_limit_display": _format_content_limit(MAX_CONTENT_BYTES),
}
```

### 2.3 前端动态限制

knowledge.js 的 Vue data 增加 `contentLimitBytes`（默认 102400 兜底），loadStats 时从 stats 响应读取。4 处硬编码 10240 替换为 `this.contentLimitBytes`。

i18n 文案改为参数化：
- content_too_large: "内容不能超过 {limit}" / "Content cannot exceed {limit}"
- file_too_large: "文件超过 {limit} 限制" / "File exceeds {limit} limit"

调用处用 `this.t('content_too_large').replace('{limit}', this.contentLimitDisplay)`。

isFileTooLarge 同理用 `this.contentLimitBytes` 替代硬编码 10240。

### 2.4 agent 写入路径加校验

在 events.py add_knowledge 中，调用 write_knowledge 前加 MAX_CONTENT_BYTES 校验：

```python
# src/api/routes/events.py add_knowledge()
from src.api.routes.knowledge import MAX_CONTENT_BYTES, _format_content_limit
if len((body.content or "").encode("utf-8")) > MAX_CONTENT_BYTES:
    raise HTTPException(status_code=400, detail={"code": 400, "message": f"content 不能超过 {_format_content_limit(MAX_CONTENT_BYTES)}"})
```

### 2.5 prompt 注入截断保护

在 _build_agent_knowledge_section 中注入知识内容时加截断，参考已有的 _PROJECT_INFO_SUMMARY_CHARS 模式：

```python
# src/prompt_builder.py
_KNOWLEDGE_INJECT_MAX_CHARS = 8000  # 单条知识注入 prompt 的最大字符数

# 在 _build_agent_knowledge_section 中：
content = read_entry_content(...)
if content:
    if len(content) > _KNOWLEDGE_INJECT_MAX_CHARS:
        content = content[:_KNOWLEDGE_INJECT_MAX_CHARS] + "\n...(已截断，可用 read tool 读取全文)"
    lines.append(f"[{scope} 级] {content}")
```

### 2.6 .env.example

新增注释行（不设值，说明不设置则使用代码默认值 100KB）：

```
# 知识正文大小上限（字节，不设置则默认 102400=100KB；非数字回退默认值，低于 1024 钳制为 1024）
#KNOWLEDGE_MAX_CONTENT_BYTES=102400
```

## 三、改动文件清单

| 文件 | 改动类型 | 说明 |
|------|---------|------|
| src/api/routes/knowledge.py | 修改 | MAX_CONTENT_BYTES 改环境变量+报错动态化+stats 附带 limit |
| src/api/routes/events.py | 修改 | add_knowledge 加 MAX_CONTENT_BYTES 校验 |
| src/prompt_builder.py | 修改 | _build_agent_knowledge_section 加截断保护 |
| src/api/static/js/pages/knowledge.js | 修改 | 4处硬编码改动态+isFileTooLarge |
| src/api/static/index.html | 修改 | bump knowledge.js?v=148→149 |
| src/api/static/locales/zh/knowledge.json | 修改 | content_too_large+file_too_large 参数化 |
| src/api/static/locales/en/knowledge.json | 修改 | 同上 |
| .env.example | 修改 | 新增注释说明 |
| tests/test_api_routes.py | 修改 | 新增测试+更新旧测试 |

## 四、风险分析

| 风险 | 影响 | 缓解 |
|------|------|------|
| 默认 100KB 影响 prompt token 消耗 | 中 | prompt 注入截断 8000 字符保护 |
| 环境变量未设置时行为变化 | 低 | 仅放宽不收紧，10KB→100KB 非破坏性 |
| 前端 stats 接口未返回 limit 时 | 低 | 前端兜底默认 102400 |
| agent 写入大知识影响存储 | 中 | 加校验限制，与 member 路径对齐 |

## 五、验收标准

- [ ] 创建超过 10KB 但 ≤100KB 的知识可成功入库（前端+后端）
- [ ] 更新超过 10KB 但 ≤100KB 的知识可成功更新（前端+后端）
- [ ] 超过 100KB 时前端拦截 + 后端拒绝，报错含"100KB"
- [ ] 设置 KNOWLEDGE_MAX_CONTENT_BYTES=2097152 后上限变 2MB
- [ ] agent 写入路径有大小校验
- [ ] 知识注入 prompt 有截断保护
- [ ] i18n 文案动态展示实际上限
- [ ] 存量 ≤10KB 知识不受影响
- [ ] 测试覆盖以上场景，现有测试全部通过
