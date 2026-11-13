# Spec: Issue #2134 — #1982 PR !1202 收尾：CI 接线 + 文档落点 + 补装死代码清理

> 承接 Issue Agent Spec v2（评论 200252364，评级 ready）。开发基准 = `arch-weekly-11-zj`
> （本流水线注入基线，实测已含 PR !1202：两个 1982 用例文件存在、`acp_bootstrap.py`
> 自动补装分支已删）。本文件把 Spec v2 的三项任务映射到该分支的实际代码入口，
> 落改前已逐一核对。

## Objective

闭环 #1982 / PR !1202 审查遗留的三项收尾：

1. 护栏用例「写而未接线」——两个 1982 回归用例文件在 `.github/workflows/` 零引用，
   回退只能靠人工 pytest 拦截；
2. 探活三态语义与服务端分账规则缺少可检索的文档落点（`docs/issue-1982/` 未建）；
3. 补装链路死代码（`_pip_install_into` / `_installed_acp_version` /
   `PIP_INSTALL_TIMEOUT` / `ACP_PACKAGE`）残留生产模块，仅测试引用，存在被后续
   读者误接回自动补装的风险。

## 实际入口确认（arch-weekly-11-zj，file:line）

| Spec 对象 | 本分支实际落点 | 结论 |
|---|---|---|
| CI 接线 | `.github/workflows/windows-smoke.yml`（115 行）`linux-backend` job 末位 step 为 issues #2028/#2041（L106–115），#1982 零引用 | 新 step 追加为 `linux-backend` 最后一个 step |
| 文档落点 | `docs/` 下 `issue-*` 目录 227 个，`issue-1982` 不存在 | 新建 `docs/issue-1982/README.md` |
| 死代码 | `src/domains/runtime/engine/acp_bootstrap.py`（539 行）：`PIP_INSTALL_TIMEOUT` L65、`ACP_PACKAGE` L66、`_installed_acp_version` L462–473、`_pip_install_into` L476–490 | 四符号删除（首选方案） |
| hint 内联 | `_ensure_hermes_acp` 的 `deps_missing` 分支 L520–527 把 `ACP_PACKAGE` 拼进人工修复提示（L522–523） | 删 `ACP_PACKAGE` 时内联字面量 `agent-client-protocol`，否则 NameError |
| 测试耦合 | `tests/runtime/test_hermes_acp_bootstrap.py`（359 行）：`TestInstalledAcpVersion` L164–177、`TestEnsureHermesAcp` 中 5 个 `patch.object(ab, "_pip_install_into")` 用例 L186–227、`TestPipInstallInto` L263–281 | 三处同步裁剪 + 补源码零引用断言 |

## In Scope（三项任务）

1. **T1 CI 接线**：`linux-backend` job 末位追加单 step，单次 pytest 同跑
   `tests/runtime/test_issue_1982_fd_leak.py` + `tests/runtime/test_issue_1982_probe_state.py`
   （`python -m pytest ... -v`），`env: GITCODE_API_TOKEN: test_token` 与同类 step 一致，
   注释标注 Issue #1982。必须接 Linux 步骤：fd 用例依赖 `/proc/self/fd`（文件内置
   skipif），Windows 步骤无效。
2. **T2 文档落点**：新建 `docs/issue-1982/README.md`，覆盖
   - 三态语义 `ok` / `spawn_failed` / `model_unreachable`（acp_bootstrap.py L68–77），
     另有两个仅诊断状态 `no_acp_subcommand` / `deps_missing`；
   - 服务端分账（runtime.py L487–559）：`ok` 清零并 healthy=1；`fail`/`model_unreachable`
     计连败、第 2 次降级并断开 spawn 链；`spawn_failed` 仅独立计数，达
     `RUNTIME_PROBE_SPAWN_FAIL_TRIP`（env 可调，默认 50）才降级并附 LimitNOFILE/ulimit
     指引；`last_probe` 重放幂等；
   - `RUNTIME_ACP_CHECK_TIMEOUT`（acp_bootstrap.py L44–61）：整数 1~300 钳制、
     非法值回退默认 20s 并告警；
   - 排查指引，行为基准指向 `tests/runtime/test_issue_1982_probe_state.py`。
3. **T3 死代码处置（首选删除）**：
   - 删除四符号；`deps_missing` hint 内联字面量包名；
   - 同步裁剪 `test_hermes_acp_bootstrap.py` 三处引用；「不自动补装」语义改由
     源码零引用断言 + 行为用例（patch `subprocess.run` 断言零调用）承载；
   - 新增源断言用例：生产路径（`src/`）对四符号零引用、模块内四符号已不存在，
     防止被后续读者接回自动补装。

## Out of Scope

- 不重新实现 #1982 的 fd 收口与三态判定逻辑（已由 !1202 落地）；
- 不调整服务端分账阈值与告警策略（阈值 2 与 50 的可配置化另立 Issue）；
- 不改动引擎恢复自动加回场景与任务绑定对账（#1982 Spec 第 6 项，另立子任务）；
- `_venv_python` / `_venv_pip` 不在删除范围（`_venv_pip` 仍用于人工修复 hint；
  `_venv_python` 有 #700 用例覆盖）。

## Acceptance Criteria（在 arch-weekly-11-zj 实跑，不接受本地推演）

- [ ] 新 step 的 run 行命令本机实跑全绿：`python -m pytest
      tests/runtime/test_issue_1982_fd_leak.py tests/runtime/test_issue_1982_probe_state.py -v`
- [ ] `docs/issue-1982/README.md` 存在，覆盖三态语义、服务端分账（含 spawn_failed
      独立计数与 50 次阈值）、`RUNTIME_ACP_CHECK_TIMEOUT` 覆盖方式
- [ ] `grep -rn "_pip_install_into\|_installed_acp_version\|PIP_INSTALL_TIMEOUT\|ACP_PACKAGE" src/`
      零命中（删除方案）
- [ ] `python -m pytest tests/runtime/test_hermes_acp_bootstrap.py -v` 全绿
- [ ] 无新增 lint/类型检查告警

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| 删 `ACP_PACKAGE` 后 hint 引用悬空 → NameError | High | hint 内联字面量 `agent-client-protocol`（T3 一并改） |
| 删除符号打断既有测试（9 个用例引用） | Med | 同 PR 内同步裁剪三处测试并补源断言用例 |
| fd 用例非 Linux 平台 skip 形成「假绿」 | Med | 固定接入 `linux-backend`，PR 说明贴 Linux 实跑日志 |
| 死代码被后续读者接回自动补装 | Med | 源码零引用断言用例进 CI（同文件接入门禁） |
