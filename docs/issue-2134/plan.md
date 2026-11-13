# Plan: Issue #2134 实现方案

> 开发分支 `feat/issue-2134`（基于 `arch-weekly-11-zj`），一个 PR、按任务分原子
> commit：spec/plan/task 文档 → T1 CI 接线 → T2 文档落点 → T3 死代码清理。

## 方案总览

三项收尾互不依赖、可独立验证，全部为最小改动：

| 任务 | 改动文件 | 改动类型 |
|------|---------|---------|
| 前置文档 | docs/issue-2134/{spec,plan,task}.md | 新增（仓库惯例） |
| T1 | .github/workflows/windows-smoke.yml | `linux-backend` 末位追加 1 个 step |
| T2 | docs/issue-1982/README.md | 新增单入口文档 |
| T3 | src/domains/runtime/engine/acp_bootstrap.py | 删 4 符号 + hint 内联包名 |
| T3 | tests/runtime/test_hermes_acp_bootstrap.py | 裁剪 3 处引用 + 补源断言用例 |

## T1 · CI 接线设计

- 追加为 `linux-backend` 的**最后一个 step**（现末位为 issues #2028/#2041，
  L106–115），不插入中间打乱既有顺序；
- step 命名沿用既有「用途 + (issue #N)」惯例；注释写明两文件此前未接 CI、
  fd 用例依赖 `/proc/self/fd` 必须走 Linux 步骤；
- `env: GITCODE_API_TOKEN: test_token` 与同 job 其他 pytest step 一致；
- 单次 pytest 同跑两个文件（`python -m pytest a.py b.py -v`），与
  issue #473/#587 等 step 形式对齐，避免重复启动 pytest；
- 验证：run 行命令在本机（Linux）原样实跑全绿。

## T2 · 文档结构设计

`docs/issue-1982/README.md` 按「三态语义 → 服务端分账 → 超时 env 覆盖 → 排查指引」
组织（Spec v2 指定结构）：

1. 三态语义表：5 个状态（3 主态 + 2 诊断态）的判定边界、典型 error、模块常量；
2. 服务端分账表：4 类观测（ok / fail+model_unreachable / spawn_failed / 重放）
   对 `probe_fail_count`、`probe_spawn_fail_count`、`healthy` 的作用，含
   `RUNTIME_PROBE_SPAWN_FAIL_TRIP`（默认 50，env 可调）与降级 hint；
3. `RUNTIME_ACP_CHECK_TIMEOUT`：1~300 钳制、非法回退默认 20s + WARNING；
4. 排查指引（LimitNOFILE / ulimit -n / 人工补装 / provider 排查）与行为基准
   （`tests/runtime/test_issue_1982_probe_state.py`、
   `tests/runtime/test_issue_1982_fd_leak.py`）。

事实基准全部与 arch-weekly-11-zj 源码核对（acp_bootstrap.py L44–89、
runtime.py L487–559），不含实现推测。

## T3 · 死代码清理设计

**选择删除（Spec 首选）**，理由：生产零调用，删除彻底消除误接回风险；
测试裁剪成本可控（9 个用例、3 处引用）。

生产侧（acp_bootstrap.py）：

- 删 `PIP_INSTALL_TIMEOUT`（L65）、`ACP_PACKAGE`（L66）两常量；
- 删 `_installed_acp_version`（L462–473）、`_pip_install_into`（L476–490）两函数；
- `deps_missing` hint（L522–523）由 `{ACP_PACKAGE}` 改为字面量
  `agent-client-protocol`（保持原输出文案不变）；
- 模块 docstring 补一行 #2134 记录（补装 helper 已随死代码清理删除）。

测试侧（test_hermes_acp_bootstrap.py）：

- 整类删除 `TestInstalledAcpVersion`（3 用例）、`TestPipInstallInto`（3 用例）；
- `TestEnsureHermesAcp` 5 个用例的 `patch.object(ab, "_pip_install_into")` 改为
  `patch("subprocess.run")`——「不自动补装」语义从「安装函数未被调用」升级为
  「底层 subprocess.run 零调用」行为断言，语义等价且不依赖已删符号；
- 新增 `TestAutoInstallRemoved`（源断言，2 用例）：
  a. `hasattr(ab, sym)` 全 False（四符号已从模块删除）；
  b. 遍历 `src/**/*.py` 源码文本，四符号名零出现（防接回自动补装）。

## 测试策略

- RED：先删生产符号 → 引用旧符号的用例必红（证明裁剪必要）；
- GREEN：同步裁剪 + 源断言后全绿；
- 回归：两个 1982 用例文件 + `test_hermes_acp_bootstrap.py` 全量绿；
- CI：新 step run 行原样本机实跑（即上条命令）。

## 提交计划（原子 commit，全英文 message）

1. `docs: #2134 record implementation spec, plan and task breakdown`
2. `ci: #2134 wire issue 1982 fd and probe-state regression tests into linux-backend`
3. `docs: #2134 add issue 1982 probe tri-state and accounting reference`
4. `refactor: #2134 remove dead acp pip-install helpers from bootstrap`

PR base = `arch-weekly-11-zj`（流水线注入基线），head = `feat/issue-2134`。
