# spec：worker 自动检查并补装 hermes ACP 依赖【已废弃 → 诊断 + 人工修复】

日期：2026-10-10　状态：**已废弃（Deprecated，2026-11-12）**　作者：Neko　仓库：grape（worker 侧改动为主）

> ## ⚠️ 废弃说明（先读这里）
>
> 本文档描述的「worker 自动补装 ACP 依赖」方案**已废弃，不再实施**。Issue #1982
> 复审后确定改为**「诊断 + 人工修复」**口径：
>
> - `src/domains/runtime/engine/acp_bootstrap.py` 现在**只做诊断**：探测 ACP 依赖
>   是否可用，缺失时输出带 error 文本的诊断信息与**人工修复命令**（提示运维执行
>   `pip install 'hermes[acp]'` 等），**不再自动 pip install**（自动补装分支
>   `_pip_install_into` 等已从生产路径删除）。
> - 探活已三态化（`ok` / `spawn_failed` / `model_unreachable`），依赖缺失、资源
>   类 spawn 故障、模型不可达分别归账，详见 Issue #1982 与
>   `tests/runtime/test_issue_1982_probe_state.py`。
> - 持续 spawn_failed 有独立计数兜底（`probe_spawn_fail_count`，连续阈值后降级
>   healthy 并在 `probe_error` 附修复指引），见 `routes/runtime.py` 心跳逻辑。
>
> 下文保留原方案的**历史动机与背景分析**（§1，仍然成立），自动补装的可执行
> 方案（§4 伪代码与步骤）已删除，仅保留结论性描述供追溯。请勿按本文实现任何
> 自动安装逻辑。

## 1. 背景与根因（历史动机，仍然成立）

- worker 安装脚本不装引擎，只提示用户自装（install-worker.sh:493-504）。
- hermes 的 ACP 依赖是可选 extra（`pip install hermes[acp]`），普通 `pip install hermes` 不带 agent-client-protocol。
- worker 的引擎健康探测只跑 `[cli, --version]`（agent.py:166）——该命令不触碰 ACP 依赖，缺依赖照样 healthy=True。
- 结果：机器心跳在线、引擎"健康"，但 Grape 用 ACP 协议调 hermes 时报 "ACP dependencies not installed"，要等任务真派下来才暴露（wanglei 机器实证）。

## 2. 目标（按废弃后的现行口径）

所有托管机器上的 hermes 引擎，worker 启动/重注册时自动：
1. 检查 ACP 依赖是否可用（诊断，只读）；
2. 缺失时输出诊断信息与**人工修复命令**，探活归类为不可用，让服务端引擎选择跳过该引擎（不再"在线但调不通"）；
3. **不做任何自动安装**——依赖修复由运维按提示手工执行。

## 3. 现有机制（核对过的落点，历史分析）

| 落点 | 现状 |
|---|---|
| grape-worker/src/runtime/agent.py `_discover_local_runtimes()` (L155-173) | 遍历引擎跑 `[cli,--version]`，输出 {name,version,healthy,cli_path,installed} |
| 同文件 `_amain()` L1255-1263 / `_reregister()` | 用上面结果调 client.register |
| grape-worker/src/runtime/client.py `register()` (L90) | payload 原样 POST /api/runtime/register |
| grape/src/api/routes/runtime.py `runtime_register` (L126-181) | 信任 worker 上报的 healthy 写入 agent_runtimes.healthy |
| engine_select.py | 选引擎过滤 only_healthy → healthy=False 即不被派任务 |
| install-worker.sh L496-497 | 提示文本 `pip install hermes（hermes 引擎）` |

结论（历史）：healthy 字段链路（worker上报→register写入→engine_select过滤）已通，只需让 worker 把 ACP 可用性纳入 healthy。

## 4. 原自动补装方案（已废弃，仅结论性描述）

原方案为在 worker 侧新增 `_ensure_hermes_acp(cli)`：`hermes acp --check` 自检 →
定位 hermes venv → 用该 venv 的 pip 安装 pin 版本的 agent-client-protocol → 复检。
**该方案不再实施，可执行步骤与伪代码已删除**——自动改写第三方引擎运行环境存在
版本漂移、离线机器反复重试、装错环境等风险，且违反"worker 不装引擎"的边界。

现行实现（Issue #1982）：
- `acp_bootstrap.py` 只诊断：check 失败时返回带 error 文本的结果并输出人工修复
  命令（如 `<hermes的venv>/bin/pip install 'hermes[acp]'`），由运维手工执行；
- `local_probe.py` 对 hermes 走 `--version` + ACP 真实握手（`acp_probe`）判定真实
  可用性，非 hermes 引擎保留 `probe_args` 覆盖；
- 探活三态（ok / spawn_failed / model_unreachable）+ `probe_spawn_fail_count`
  独立计数兜底，见 `routes/runtime.py`。

## 5. 触达面/兼容性（历史分析，现行实现等同）

- 非 hermes 引擎（opencode/codearts/cursor）：零行为变化。
- 已装 hermes 且 ACP 依赖齐的机器：多一次 <1s 的自检子进程，无副作用。
- 缺依赖的存量机器：如实注册为不可用（诊断信息含修复命令），从"假健康"变"如实不健康 + 可执行的人工修复指引"。

## 6. 测试（现行口径）

- `tests/runtime/test_hermes_acp_bootstrap.py`：诊断路径（check 通过/失败、不触发安装）。
- `tests/runtime/test_issue_1982_probe_state.py`：探活三态语义与 spawn_failed 计数兜底。
- `tests/insight/test_local_probe.py`：本机直连探活（含 probe_args 覆盖）。

## 7. 交付物（历史记录）

原方案交付物清单已随废弃失效；现行对应实现见 Issue #1982 的 PR 变更清单
（acp_bootstrap.py / agent.py / local_probe.py / routes/runtime.py / install-worker.sh 及测试）。
