# spec：worker 自动检查并补装 hermes ACP 依赖

日期：2026-10-10　状态：待批准　作者：Neko　仓库：grape（worker 侧改动为主）

## 1. 背景与根因

- worker 安装脚本不装引擎，只提示用户自装（install-worker.sh:493-504）。
- hermes 的 ACP 依赖是可选 extra（`pip install hermes[acp]`），普通 `pip install hermes` 不带 agent-client-protocol。
- worker 的引擎健康探测只跑 `[cli, --version]`（agent.py:166）——该命令不触碰 ACP 依赖，缺依赖照样 healthy=True。
- 结果：机器心跳在线、引擎"健康"，但 Grape 用 ACP 协议调 hermes 时报 "ACP dependencies not installed"，要等任务真派下来才暴露（wanglei 机器实证）。

## 2. 目标

所有托管机器上的 hermes 引擎，worker 启动/重注册时自动：
1. 检查 ACP 依赖是否可用；
2. 缺失则自动补装 agent-client-protocol==0.9.0；
3. 补装后仍不可用的，注册时 healthy=False，让服务端引擎选择直接跳过该引擎（不再"在线但调不通"）。

## 3. 现有机制（核对过的落点）

| 落点 | 现状 |
|---|---|
| grape-worker/src/runtime/agent.py `_discover_local_runtimes()` (L155-173) | 遍历引擎跑 `[cli,--version]`，输出 {name,version,healthy,cli_path,installed} |
| 同文件 `_amain()` L1255-1263 / `_reregister()` | 用上面结果调 client.register |
| grape-worker/src/runtime/client.py `register()` (L90) | payload 原样 POST /api/runtime/register |
| grape/src/api/routes/runtime.py `runtime_register` (L126-181) | 信任 worker 上报的 healthy 写入 agent_runtimes.healthy |
| engine_select.py | 选引擎过滤 only_healthy → healthy=False 即不被派任务 |
| install-worker.sh L496-497 | 提示文本 `pip install hermes（hermes 引擎）` |

结论：**服务端零改动**。healthy 字段链路（worker上报→register写入→engine_select过滤）已通，只需让 worker 把 ACP 可用性纳入 healthy。

## 4. 改动方案（全部在 worker 侧）

### 4.1 新增 `_ensure_hermes_acp(cli) -> bool`（agent.py，~40行）

```
def _ensure_hermes_acp(cli: str) -> bool:
    # ① 检查：hermes acp --check（官方自检命令，退出码0=依赖齐）
    # ② 缺失则定位 hermes 的 venv：读 launcher 脚本里的 exec "...venv/bin/hermes" 行，
    #    提取 venv 路径（本机实测格式：#!/usr/bin/env bash\nexec ".../venv/bin/hermes" "$@"）
    # ③ 用该 venv 的 pip 装 agent-client-protocol==0.9.0（装进 hermes 自己的环境，
    #    不是 worker 的环境、也不是系统环境——系统装了对 hermes 无效）
    # ④ 复跑 hermes acp --check，返回最终结果
    # 任一步失败：log.warning + return False（不抛异常，不阻塞其他引擎注册）
```

要点：
- 每步 subprocess 均带 timeout（check 15s / pip install 120s）。
- 版本 pin ==0.9.0：与 hermes pyproject 的 acp extra 一致，避免将来 agent-client-protocol 破坏性升级。
- pip 不可用（纯 uv 环境）时尝试 venv/bin/python -m pip；仍失败按 ④ 的 False 走。

### 4.2 接入 `_discover_local_runtimes()`（改 ~8行）

现有循环里对 `r["name"]=="hermes"` 且已装的引擎追加：

```
acp_ok = _ensure_hermes_acp(cli)
healthy = version_probe_ok and acp_ok
if not acp_ok:
    log.warning(f"hermes ACP 依赖缺失且自动补装失败，注册为 unhealthy: {cli}")
```

本机直连引擎（local_probe.py）同步改：hermes 的 probe_args 由 ["--version"] 改为 ["acp","--check"]（该字段已存在、就是为覆盖探测命令设计的，一行改动）。

### 4.3 提示文本修正（install-worker.sh / .ps1，各1行）

`pip install hermes（hermes 引擎）` → `pip install 'hermes[acp]'（hermes 引擎，含 ACP 协议包）`

### 4.4 不做的事（边界）

- 不改服务端 /api/runtime/register 及任何表结构（healthy 语义够用）。
- 不在心跳循环里反复检查（--check 每次 spawn 一个 hermes 进程，有心跳频率就有成本；只在启动注册和 _reregister 时做，与现有探测节奏一致）。
- 不自动升级 hermes 本体、不装其他引擎的依赖。
- 补装失败不重试不停机：healthy=False 注册上去，日志里写清"请人工执行 <venv>/bin/pip install agent-client-protocol==0.9.0"，符合"失败即终止报出来"哲学。

## 5. 触达面/兼容性

- 非 hermes 引擎（opencode/codearts/cursor）：零行为变化。
- 已装 hermes 且 ACP 依赖齐的机器（如 mys）：--check 直接通过，多一次 <1s 的子进程，无副作用。
- 缺依赖的存量机器（如 wanglei）：worker 下次重启/重注册时自动补装修复，无需人工。
- 纯部署回放（pip 失败、离线）：降级为 healthy=False + 警告日志，与现状相比只是从"假健康"变"如实不健康"。

## 6. 测试计划（只跑相关文件）

- 新增 tests/test_hermes_acp_bootstrap.py：
  - mock subprocess 验证 --check 通过时不尝试安装；
  - --check 失败 → 解析 launcher 提取 venv → pip install 命令正确、pin 版本正确；
  - pip 失败 → 返回 False 且不抛异常；
  - _discover_local_runtimes 集成：hermes acp 失败时 healthy=False。
- 手工验证：本机 `hermes acp --check` 已 OK（回归确认注册仍 healthy）。
- 不跑全量，不碰无关测试。

## 7. 交付物

1. grape-worker/src/runtime/agent.py（+~45行）
2. grape-worker/src/runtime/local_probe.py（~2行）
3. grape/scripts/install-worker.sh、install-worker.ps1（各1行）
4. grape-worker/tests/test_hermes_acp_bootstrap.py（新增）

预估总量 ~120行，一次提交完成（先在个人分支 mys-local commit，是否 push/MR 听主人指示）。
