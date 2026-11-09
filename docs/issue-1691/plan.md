# Plan: Issue #1691 — a2a handoff 405 指引与派发校验

## 现状

- 服务端 A 进程 a2a 路由、405 `detail.message` 信封由 PR #1071（base=weekly-10）提供，本单不重复。
- grape-cli `ApiClient` 已能解析 `{"detail":{"code","message"}}`，但没有 a2a 专属提示、fallback base 与 handoff 结果校验。
- coding-cli runbook 只有 `grape a2a handoff` 调用示例，没有三服 base 口径与失败判定。

## 改动

### grape-cli

1. `src/config.py`
   - 新增 `a2a_fallback_base_url()`：`GRAPE_A2A_FALLBACK_BASE_URL` > `~/.grape/config.json` 的 `a2a_fallback_base_url`。
2. `src/client.py`
   - `_is_a2a_path()` 识别 `/api/a2a`、`/api/a2a/status`、`/api/pipeline/handoff`。
   - a2a 路径 405 且配置了 fallback base 时，自动切到 fallback 重试一次；成功后客户端 base 固定为 fallback。
   - 405 错误附带当前 base 与切换事件服/公共 base 的提示；服务端 `detail.message` 原样保留。
3. `src/registry.py`
   - 对 `a2a_handoff` / `pipeline_handoff` 响应做统一校验：
     - `status != ok` 或 `event.status == failed` → `CliError`。
     - `seq <= 0` 且无 `skipped` / `queued` → `CliError`（未建派发事件）。
     - 有 `event` → 直接写入 `downstream`；否则回查 `GET /api/a2a/status` 获取下游状态。
4. 测试：`test/test_client.py` 覆盖 405/fallback；`test/test_cli.py` 覆盖 handoff 校验与 status 回查。

### grape

1. `src/api/routes/webhook.py`
   - 新建事件且派发成功时，给响应补 `event: {seq,status,agent,issue_id}`（幂等/排队响应已有同样字段，口径一致）。
2. 测试：`tests/test_issue_1691_a2a_dispatch_status.py` 直接验证响应补全（打桩鉴权/DB/派发）。

### grape-skills

1. `skills/coding/ai-coding-review/coding-cli/references/a2a-handoff-base-url.md`：三服职责边界表、默认 base 来源、推荐 base、405 处置、seq/派发校验。
2. `coding-runbook.md` Step 7 / Step 3b 引用该文档并增加失败判定与重试口径。

## 风险

| 风险 | 缓解 |
|------|------|
| fallback 切服误发业务请求 | 仅 a2a 路径、仅 405、仅显式配置 |
| 旧服务端无 `event` 字段 | 回查 `/api/a2a/status` 兜底 |
| 与 PR #1071 冲突 | 不修改其覆盖的 a2a_router/405/ops/12-a2a 文档 |