# Spec: Issue #423 — hermes ACP 探活 `_read_response` 10 行上限误报 fail

> 来源：Issue #423 正文 + Issue Agent Spec（评级 ready），coding agent 开发基线。

## 目标
- `_probe_hermes_acp` 的 `_read_response` 由「固定 10 行」改为 **deadline 驱动**：deadline 内持续读取，行数仅作防御性上限（500），select 超时/EOF 立即返回。
- `session/prompt` 返回 `stopReason=refusal` 时返回独立状态 `refused`，与 `fail`（链路不通）区分。
- 单测覆盖「通知 ≥10 行」会话形态；同步审查 `_probe_hwcloud_acp`。

## 范围
### In Scope
- `src/runtime/agent.py` `_probe_hermes_acp._read_response`：deadline 驱动 + 500 行防御上限。
- `src/runtime/agent.py` hermes/hwcloud prompt 响应 `stopReason=refusal` → `status="refused"`。
- `tests/test_probe_acp_read_response_423.py`：fake ACP CLI 端到端探活测试。
- `_probe_hwcloud_acp` 审查：deadline 驱动已满足（while deadline 循环，无行数上限），补 500 行防御上限与 refusal 标记保持同构。

### Out of Scope
- hermes 两步→三步（session/new）探活流程重做（Issue 明示由后续单独重做）。
- 服务端 probe_status 联动逻辑（`refused` 走 runtime.py 既有 else 分支：记录但不计入连败摘除，无需改动）。
- 错误文案体系重构。

## 验收标准
- [ ] 通知 ≥10 行（34+ 行 session/update）会话探活返回 ok，不误报 fail
- [ ] `_read_response` 无 10 行硬上限；deadline 驱动；select 超时/EOF 立即返回 None
- [ ] `stopReason=refusal` → `status="refused"`（≠ fail），上层不计入摘除
- [ ] 超过 500 行防御上限返回 None（防失控流）
- [ ] 单测全绿，现有测试回归通过
