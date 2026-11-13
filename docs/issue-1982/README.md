# Issue #1982：ACP 探活三态语义与服务端分账

> hermes ACP 健康探测的行为基准文档（Issue #1982 / PR !1202 落地，Issue #2134 补文档落点）。
> 行为以测试为基准：`tests/runtime/test_issue_1982_probe_state.py`（三态 + 服务端分账）、
> `tests/runtime/test_issue_1982_fd_leak.py`（fd 收口），两者已接入
> `windows-smoke.yml` 的 `linux-backend` job（CI 门禁）。
> 代码入口：`src/domains/runtime/engine/acp_bootstrap.py`（探测与三态判定）、
> `src/domains/runtime/routes/runtime.py`（`runtime_heartbeat` 服务端分账）。

## 一、为什么是三态

早期实现用 `hermes --version` 判健康，探测不出 ACP 依赖缺失（不触碰 ACP 模块），
导致引擎注册为 healthy 但 ACP 调用永远失败；更早的 `hermes acp --check` 自检子命令
在 hermes v0.9.0 起不存在，探测恒失败（Issue #613）。现改为对 `hermes acp` 做真实
ACP 握手（stdin 发 `initialize` + `session/new` 两条 JSON-RPC 请求，stdout 收到
JSON-RPC 响应即视为可用），并把失败原因分类——**环境/资源故障（子进程起不来）与
模型/协议故障（进程起了但不可达）必须分开**，否则 fd 打满（`[Errno 24]`）这类
环境故障会被错误归因为「引擎不可用」并触发摘除，或被误判为「依赖缺失」触发不可
执行的自动补装（Issue #1982 已删除自动补装链路，只诊断）。

## 二、探活三态语义（acp_bootstrap.py）

`_acp_probe_detailed(cli) -> {healthy, status, reason, error}`，status 取值：

| status | 语义 | 判定边界 | 典型 error |
|---|---|---|---|
| `ok` | 握手成功，ACP 依赖可用 | stdout 收到 JSON-RPC 响应行（含 `id`/`result`/`error`/`method` 键） | — |
| `spawn_failed` | 子进程起不来（环境/资源故障），**不计入探活连败** | `Popen` 抛 OSError 且命中资源类特征：errno EMFILE/ENOMEM/EAGAIN，或文本含 `too many open files` / `errno 24` / `cannot allocate memory` / `resource temporarily unavailable`；进程已起但 stderr 命中上述特征同判 | `OSError: [Errno 24] Too many open files` |
| `model_unreachable` | 协议层不可达（进程已起、无 JSON-RPC 响应） | 等待超时无响应行、stderr 无其他可分类特征、或模型/provider 侧错误 | `no JSON-RPC response within 20s` |
| `fail` | 其他失败 | 非 JSON-RPC 输出、未知启动异常（不属上述任何类） | 首行输出 garbage |

另有两个**仅诊断**状态（不触发摘除，也不补装）：

| status | 语义 | 典型 error |
|---|---|---|
| `no_acp_subcommand` | hermes 版本过旧、无 `acp` 子命令 | `unrecognized arguments` / `unknown command` / `invalid choice` |
| `deps_missing` | ACP 依赖缺失（agent-client-protocol 未装） | `ModuleNotFoundError: No module named 'agent_client_protocol'` |

诊断提示均为**人工修复**指引（不自动补装）：`no_acp_subcommand` → 升级 hermes；
`deps_missing` → 在 hermes 所属 venv 人工执行 `pip install agent-client-protocol`。

调用方：worker 侧 `local_probe._probe_one` 对 hermes 引擎走 `_acp_check(cli, timeout=剩余预算)`
（不超过单引擎 `LOCAL_PROBE_TIMEOUT`），引擎注册走 `_ensure_hermes_acp`。

## 三、服务端分账（runtime.py `runtime_heartbeat`）

worker 心跳携带 `probe_results`（每引擎 `{status, latency, last_probe, error}`），
服务端按 status 分账写 `agent_runtimes` 表：

| 观测 | `probe_fail_count` | `probe_spawn_fail_count` | `healthy` |
|---|---|---|---|
| `ok` | 清零 | 清零 | 置 1（自动恢复） |
| `fail` / `model_unreachable` | +1，**连败 ≥2 次置 healthy=0**（阈值硬编码 2） | 清零（进程能 spawn，spawn 连败链断开） | 第 2 次起置 0 |
| `spawn_failed` | 不动（不计连败） | +1 | 达到 `_PROBE_SPAWN_FAIL_TRIP`（默认 50）才置 0 |
| `last_probe` 相同（重放） | 不动 | 不动 | 不动（幂等，仅刷新 `probe_status/latency/last_probe/error`） |
| 其他（skip/pending/refused 等） | 不动 | 不动 | 不动（保留 `--version` 探测结果） |

- **`fail` / `model_unreachable` 摘除语义**：模型/协议类故障两次连败即摘除引擎
  （不接单）；`model_unreachable` 额外打 WARNING 日志。
- **`spawn_failed` 兜底语义**：偶发资源紧张不摘除引擎（不误伤），但持续资源故障
  不能永远 healthy 继续接单——独立计数连续达到 `_PROBE_SPAWN_FAIL_TRIP` 次后降级
  `healthy=0`，并在 `probe_error` 追加修复指引：
  `spawn_failed x{N}: persistent resource exhaustion (EMFILE/ENOMEM/EAGAIN); fix: raise
  fd limit (LimitNOFILE=524288 / ulimit -n), free memory, restart worker; engine
  re-enabled automatically on next ok probe`。
- **恢复**：任何一次 `ok` 探活清零两个计数并恢复 `healthy=1`；`fail`/`model_unreachable`
  说明进程已成功 spawn，会同步清零 `probe_spawn_fail_count`（断开 spawn 连败链）。
- **重放幂等**：同一观测（相同 `last_probe`）重放只刷新状态字段，不递增/重置计数、
  不动 healthy（避免心跳重放导致计数虚增与误摘除，Issue #490）。

阈值相关环境变量：

| env | 默认 | 语义 |
|---|---|---|
| `RUNTIME_PROBE_SPAWN_FAIL_TRIP` | 50 | `spawn_failed` 连续多少次后降级 `healthy=0`（`max(1, int(...))` 下限 1） |

## 四、探测超时：`RUNTIME_ACP_CHECK_TIMEOUT`

ACP 握手等待响应的上限（`acp_bootstrap.ACP_CHECK_TIMEOUT`，模块级生效）：

- 默认 **20 秒**（`ACP_CHECK_TIMEOUT_DEFAULT`，含 Windows 冷启动余量——15s 在
  Windows 冷启动实测易吃满导致 healthy 震荡，Issue #700）；
- 覆盖方式：env `RUNTIME_ACP_CHECK_TIMEOUT` 设**正整数**，取值钳制在 **1~300**；
- 非法值（非数字 / 非正整数 / 超出 1~300）**不抛异常**，回退默认 20s 并打 WARNING
  日志（对齐 `agent.py` 的 env 覆盖先例）；
- worker 侧 `local_probe` 对每引擎探测传**剩余预算**（保证单引擎总时长不超过
  `LOCAL_PROBE_TIMEOUT`），不直接用该值。

## 五、排查指引

| 现象（probe_error / 日志） | 方向 |
|---|---|
| `spawn_failed` + `Too many open files` / `Errno 24` | fd 打满：`ulimit -n` 调大、`lsof -p <worker pid> \| wc -l` 定位泄漏；systemd 部署确认 `LimitNOFILE`（安装器模板为 524288）；释放内存 / 重启 worker |
| `cannot allocate memory` / `resource temporarily unavailable` | 内存或进程数受限（ENOMEM/EAGAIN），同上按资源故障处理 |
| `deps_missing`（`No module named 'agent_client_protocol'`） | 人工在 hermes 所属 venv 执行 `pip install agent-client-protocol`（Issue #1982 起不自动补装） |
| `no_acp_subcommand` | 升级 hermes 至支持 ACP 的版本 |
| `model_unreachable` | 检查 provider/model 配置与网络连通性（非依赖缺失，不补装） |
| 其他 `fail` | 手工执行 `<cli> acp` 复现，检查 hermes 版本/依赖/配置 |

## 六、相关 Issue / PR

- #1982 / PR !1202：三态化 + fd 收口 + 删除自动补装（本文档的行为来源）
- #2134：本文档落点 + CI 接线 + 补装死代码清理
- #613 / #196：`acp --check` 自检子命令不存在的历史断点
- #700：探测超时默认 20s 与 env 覆盖（Windows 冷启动震荡）
- #490：`last_probe` 重放幂等
