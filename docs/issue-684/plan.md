# Issue #684 方案设计

## 背景分析
- 服务端路由经 FastAPI 序列化：默认 `JSONResponse.render` 用 `json.dumps(ensure_ascii=False)`，控制字符本身会被转义，但存在两个真实风险窗口：
  1. **孤立代理项（lone surrogate）**：上游 GitCode/GitHub 返回的字符串若含 `\ud800` 等孤立代理项，`json.dumps(ensure_ascii=False).encode("utf-8")` 直接抛 `UnicodeEncodeError` → 500。
  2. **无统一保证**：任何新增/旁路响应（自定义 Response、StreamingResponse、后续新端点）都没有“输出必须 json.loads 可解析”的统一校验，缺陷只能逐端点发现（Issue #684 即此类）。
- milestones：GitCode 上游返回条目 `state=active`，而过滤合法值为 open/closed/all，口径错位。

## 方案

### 1. 统一 JSON 序列化出口（src/api/json_safe.py 新增）
- `SafeJSONResponse(JSONResponse)`：覆盖 `render()`：
  - 用 `json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"))` 序列化（控制字符 \x00-\x1f/\x7f 由此统一转义为 \uXXXX/\n 等合法转义序列，输出按 json.dumps 语义恒为合法 JSON）；
  - **防御降级**：encode UTF-8 抛 `UnicodeEncodeError`（孤立代理项）→ 降级 `ensure_ascii=True` 重渲染（所有非 ASCII 与孤立代理项转为 \uXXXX 转义，保证字节流合法 UTF-8 且可解析，修复历史 500）；
  - 辅助 `dumps_safe(content) -> bytes` 供复用与单测。
- `src/api/main.py`：`FastAPI(..., default_response_class=SafeJSONResponse)` —— 所有端点（含 git-repo 全部 `--json-out` 端点）共用此单一出口，非逐端点散修。

### 2. milestones state 口径收口（src/api/git_repo_api.py）
- 新增 `_normalize_milestone_state(v)`：`active`/`opened`→`open`，`closed`→`closed`，其余原样。
- `list_milestones` / `create_milestone` / `update_milestone` 返回值经 `_normalize_milestone_states(data)`（dict 或 list 递归到条目级，仅映射 `state` 字段，其他字段不动）。
- 过滤参数 `--state` 保持 open/closed/all（上游口径）；端点 description 与 docs/openapi/10-git-repos.md 注明“返回 state 归一化为 open/closed（active→open）”。

## 测试计划
- `tests/test_json_safe.py`：响应类单测（控制字符 / 原始换行 / 孤立代理项 / 嵌套结构 / 非 dict 内容）。
- `tests/test_git_repo_api.py` 增补：mock 上游返回含控制字符 body 的 PR body → app 级响应文本 `json.loads` 成功；milestones 返回 active→open；create/update 同口径；`--state` 透传不回归。

## 风险与兼容
- default_response_class 只影响“未显式指定 response_class”的端点；现有显式 JSONResponse/文件流响应不受影响。
- milestones 归一化仅改返回字段命名，不改上游数据；CLI/技能按 open/closed 消费（与过滤值一致）。
