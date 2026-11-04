# Issue #976 方案设计

## 方案：grape 侧 ACP 语义兼容读取（治本于本仓）

双向补齐中引擎侧（acp_adapter/tools.py）属外部 hermes 包，本仓治本路径为接收端兼容：

### 1. 入参提取（`_extract_tool_call_input`）
优先级：`rawInput` → `input` → `parameters`（dict 或 JSON 字符串）→ content 块解析：
- diff 块单条：write 语义还原 `{path, content}`（oldText 为 null）；patch 语义还原 `{path, old, new}`
- diff 块多条：`{edits: [...]}`；text 块：`{text: ...}`

实测（hermes 0.20.0）：`build_tool_start("tc","write_file",{path, content=57KB})` 序列化后 rawInput=None、content diff 块携带完整 57KB newText，经本提取器完整还原。

### 2. 占位事件
tool 名解析（title 推断 + name）皆空时，产出 `{"tool":"unknown","input":<提取入参>,"title":<title>,"tool_call_id":...}`，事件 schema 与既有 tool_use 一致（仅新增可选键），不静默丢弃。

### 3. 裁剪契约（`_clip_tool_input`）
序列化字节超过 `HERMES_TOOL_INPUT_MAX_BYTES`（默认 256KB，57KB 验收值在保真区间）时输出 `{truncated:true, original_bytes, sha256_prefix(16hex), preview(2KB)}`，禁止无标记截断。

## 测试方案（tests/test_hermes_acp_tool_call_input.py）
1. 57KB 保真：rawInput 路径、content diff 块路径、完整 JSON-RPC wire 行、真实 hermes 引擎 build_tool_start 往返（引擎缺席时 skip）。
2. 占位事件：name/title 皆空、"✅" 前缀 title 两种形态。
3. 端到端：handle_line → report_messages 落库 → `GET /api/task-logs/{id}/messages`（TestClient + JWT）取回 57KB 完整入参。
4. 裁剪契约 + env 上限覆盖。

## 风险
- 引擎未来版本若补传 rawInput，本方案优先读取 rawInput，天然兼容。
- 占位事件下游仅按 tool/input 消费（`_normalize_message`），新增键不影响。
