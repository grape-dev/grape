# Plan: Issue #423

## 根因
`_probe_hermes_acp._read_response` 循环为 `for _ in range(10)`：三步探活会话中 prompt 响应前 hermes 输出 36 行 stdout（34 行 session/update 通知），第 10 行耗尽后返回 None → 误判「无响应」→ 12 台 worker 被连败摘除。行数上限与「会话通知量」动态值强耦合，属脆弱设计。

## 方案
1. **deadline 驱动读取**：`while now < deadline and lines < 500`；select 超时用剩余时间（耗尽即返回 None）；EOF（readline 空）立即返回 None。500 行仅为防御失控流的保险丝。
2. **refusal 独立标记**：prompt 响应 `stopReason == "refusal"` → `{"status": "refused"}`（协议活着但模型拒绝）。服务端 `src/api/routes/runtime.py` 心跳落库对非 ok/fail 状态走 else 分支：仅记录 probe_status，不增 probe_fail_count、不动 healthy → 不触发摘除，语义正确，无需服务端改动。
3. **hwcloud 同步审查**：`_probe_hwcloud_acp._read_response` 已是 `while deadline` 无行数上限（无同类 10 行 bug）；为同构补 500 行防御上限 + refusal 标记。

## 测试（TDD）
fake ACP CLI（可执行 python 脚本，stdin/stdout JSON-RPC）端到端驱动探活：
- 34 行 session/update 通知后返回响应 → ok（旧代码在此形态必失败）
- stopReason=refusal → refused（与 fail 区分）
- EOF / 无响应（monkeypatch 短超时）→ fail
- 600 行通知 → 防御上限触发 → fail（None）
- hwcloud：initialize → session/new → prompt 全流程含 12+ 行通知 → ok / refused

## QA 复核追加修复（Optional#1，2026-10-22 实测升级）
QA 对 PR #590 三条 Optional 实测复核：#2（EOF 竞争）300 次未复现维持不改；#3（CI 覆盖）确认为 CI 增强；**#1 实测确认为真实缺陷**——select 就绪后 `readline()` 遇半行未换行数据阻塞在 read 系统调用，deadline 只约束 select 管不到 readline（实测 deadline=2s 探活 >10s 不返回，finally 兜底执行不到，worker-engine-probe 线程永久停摆）。属既有模式非本 PR 引入，但本 PR 主题即探活可靠性，一并修复：

- **修复方式**：`_read_response` 的 `readline()` 改为非阻塞 fd（`os.set_blocking(fd, False)`，标准可移植接口，避免部分构建无 `os.fcntl` 顶层别名）+ `os.read(fd, 65536)` 小块读 + `bytearray` 行缓冲拼行；每轮 select/read 后重检 deadline，半行悬置数据到 deadline 即返回 None。`_probe_hermes_acp` 与 `_probe_hwcloud_acp` 同构修复。
- **验证**：修复前实测复现挂起（>10s）；修复后同场景 1.0s 按 deadline 返回 fail，finally 终止逻辑正常执行。
- **新增回归**：hermes/hwcloud 各 +2 例——半行无换行挂起形态（断言 <3s 返回 fail）、分块到达拼行（行缓冲不丢数据）。合计 15 例全绿。
- **范围外**：`src/observability/engine_probe.py` 另一份独立 `_probe_hermes_acp` 存在同款 readline 阻塞暴露面（仅 initialize、3 行循环、text 模式），不在本 Issue 范围，QA 已建议后续 issue 跟进。

## 涉及文件
- `src/runtime/agent.py`（_probe_hermes_acp / _probe_hwcloud_acp）
- `tests/test_probe_acp_read_response_423.py`（新增）
