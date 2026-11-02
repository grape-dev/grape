# Spec: Issue #700 — Windows hermes 引擎 healthy 震荡

> 承接 Issue Agent 已发布的 Spec（见 Issue #700 评论），本文件为 coding 环节工作基线副本。

## 根因

healthy 双写源打架：

- **写入源 A（re-register，每 300s，压成 0）**：worker 主循环 `src/runtime/agent.py:2684` 每轮调 `_reregister()` → `_discover_local_runtimes()` → hermes healthy 由 `_ensure_hermes_acp()`（`acp_bootstrap.py:214`）决定：ACP 握手探测 `_acp_probe()` 超时 `ACP_CHECK_TIMEOUT=15s`（`acp_bootstrap.py:31`）。Windows 冷启动（.exe 启动器 + 大量 import）实测易吃满 15s → 探测超时归为 `ACP_FAIL` → healthy=False。注册接口 `src/api/routes/runtime.py:194` 无条件 `UPDATE agent_runtimes SET healthy=? ...` 把 DB 打成 0。
- **写入源 B（心跳探活，每 600s，升回 1）**：worker 后台探测线程每 `RUNTIME_PROBE_INTERVAL`（600s）真跑一次 `hermes acp`（Windows 专用读线程实现，分步超时 15/15/30s，无 15s 总闸），成功后心跳上报，服务端 `runtime.py:294-300` 把 healthy 升回 1。

净效果：两台 Windows hermes（17052/16958）反复横跳、大部分时间为 0；`healthy=1` 过滤路径（engine_select / scenes 绑定 / webhook / chat 引擎列表）落入 false 窗口被拒（"不能通过 Grape 调用"）。

附带：`_hermes_venv_bin()`（`acp_bootstrap.py:148-181`）对 .exe/.cmd/.bat 启动器直接返回空串 → `_ensure_hermes_acp` 无法定位 venv，缺依赖时 Windows 无法自愈补装。

## 改动范围（In Scope）

1. **`src/api/routes/runtime.py`（register 接口）**：UPDATE 增加降级守卫——已 healthy 且探活新鲜（probe_status='ok' 且 last probe ≤ 2×RUNTIME_PROBE_INTERVAL）的行，re-register 上报 False 不降级（其余字段照常更新）；探活真失败（连续失败达阈值）时心跳路径仍能正确降 0（不掩盖真实故障）。
2. **`src/runtime/acp_bootstrap.py`**：`ACP_CHECK_TIMEOUT` 默认 15→20s；支持 `RUNTIME_ACP_CHECK_TIMEOUT` 环境变量覆盖（正整数合法性校验，非法值回退默认并告警）。
3. **`src/runtime/acp_bootstrap.py` `_hermes_venv_bin()`**：新增 Windows 启动器解析——.exe/.cmd/.bat：
   - `.cmd`/`.bat` 内容解析：`@py -3 ...` / `@"python 绝对路径" ...` / `@python ...` 行提取解释器，配合 `venv\Scripts` 目录形态拼 venv bin；
   - venv python 存在性校验（`python.exe`）；
   - 解析失败回退现有人工修复日志行为（返回空串，不抛异常）。
4. **测试**：震荡回归测试（register 降级守卫各分支 + 心跳真实降级不掩盖）+ 超时覆盖单测 + 启动器解析单测；`test_issue_490_probe_idempotent.py` / `test_issue_471_reregister_robustness.py` 回归不破。

## Out of Scope

- 不改心跳探活路径本身（分步超时/Windows 读线程保持现状）。
- 不改 engine_select/scenes/webhook 的 healthy=1 过滤语义。
- 不做非 Windows 行为变更（Linux 路径行为不变）。

## 测试 seam

- register/heartbeat 端点函数直接调用（同 test_issue_490 先例：`runtime_register(body, request, credential_id=cid)` 绕过 HTTP 鉴权）。
- `_hermes_venv_bin` 纯函数：tmp 目录伪造 .cmd/.bat/.exe 文件。

## 验收标准

- 已 healthy 且探活新鲜的行，re-register 上报 False 时 DB 不降级（回归测试覆盖）
- 探活真失败（连续失败达阈值）仍能正确降 0
- `ACP_CHECK_TIMEOUT` 默认 ≥20s，`RUNTIME_ACP_CHECK_TIMEOUT` 可覆盖且非法值安全处理
- `_hermes_venv_bin()` 对 .exe/.cmd/.bat 返回有效 venv python 路径（单测覆盖 `@py -3` 与绝对路径两种 .cmd 形态）
- `test_issue_490_probe_idempotent.py` 与 `test_issue_471_reregister_robustness.py` 回归通过
