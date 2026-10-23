# Plan & Task: Issue #441 — worker hermes 探针 session/new 先行

> 方案 A（探针对齐 hwcloud 三步先例），详见同目录 spec.md。

## Plan（实现路径）

1. **改 `_probe_hermes_acp`（src/runtime/agent.py）**
   - initialize（id=1）保持不变。
   - 新增第 2 步 session/new（id=2）：params `{"cwd": tempfile.mkdtemp(prefix="hermes-probe-"), "mcpServers": []}`；无响应/error → fail（错误前缀 `ACP session/new: ...`）。
   - prompt 改 id=3、params.sessionId = session/new 响应 `result.sessionId`；sessionId 为空 → fail（不重蹈空 sessionId 覆辙）。
   - 成功 output 文案：`ACP initialize+session/new+prompt ok (stopReason=...)`；refused 口径不变。
   - finally 清理补临时 cwd 目录删除（rmtree ignore_errors）。
2. **新增测试 tests/test_probe_hermes_session_new_441.py**
   - 复用 #423 的 fake ACP CLI 模式（stdin/stdout JSON-RPC 脚本），新增「记录收到的请求」能力：把每个请求按序写入 sidecar 文件，测试断言请求序列 = initialize → session/new → session/prompt 且 prompt 的 sessionId == session/new 返回的真实值（端到端，非快照）。
   - 覆盖 T1~T6（见 spec.md §4）。
3. **回归**：`tests/test_probe_acp_read_response_423.py` 全量。

## Task（勾选）

- [x] T-1 `_probe_hermes_acp` 插入 session/new + 真实 sessionId prompt
- [x] T-2 session/new 失败分支归类 fail（无响应/error/缺 sessionId）
- [x] T-3 新增测试文件，6 用例全过
- [x] T-4 #423 回归套件全过
- [x] T-5 docs/issue-441 三件套

## 提交

- `fix: #441 worker hermes 探针 prompt 前先 session/new 获取真实 sessionId`
- PR base = `功能分支`（GRAPE_WORK_BRANCH 注入值），head = `feat/issue-441`
