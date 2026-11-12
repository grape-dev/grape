# Task Breakdown: Issue #2134

> 每项独立可验证；验收命令一律在 `arch-weekly-11-zj` 基线（feat/issue-2134 分支）
> 实跑。本地解释器：`/opt/python311/bin/python3.11`（3.11.9，含 pytest 9.2），
> 与 CI Python 3.12 同代际。

## 前置

- [x] 基线核对：两 1982 用例文件存在；`acp_bootstrap.py` 539 行、补装分支已删；
      workflow 115 行、#1982 零引用；`docs/issue-1982/` 未建
- [x] 基线测试：两个 1982 文件 14 用例全绿；`test_hermes_acp_bootstrap.py` 36 用例全绿
- [x] docs/issue-2134/{spec,plan,task}.md 落盘（commit 1）

## T1 · CI 接线（commit 2）

- [ ] `windows-smoke.yml` `linux-backend` 末位追加 step
      `Issue #1982 probe tri-state + fd regression tests`，注释标注 #1982/#2134、
      `/proc/self/fd` 仅 Linux 生效
- [ ] env `GITCODE_API_TOKEN: test_token`；run 行 =
      `python -m pytest tests/runtime/test_issue_1982_fd_leak.py tests/runtime/test_issue_1982_probe_state.py -v`
- [ ] YAML 结构校验（step 缩进 / job 归属 linux-backend）
- [ ] run 行命令原样实跑全绿

## T2 · 文档落点（commit 3）

- [ ] 新建 `docs/issue-1982/README.md`
- [ ] 覆盖：三态语义（5 状态表）+ 服务端分账（含 spawn_failed 独立计数 50 阈值、
      last_probe 幂等）+ `RUNTIME_ACP_CHECK_TIMEOUT`（1~300 / 默认 20s / 非法回退告警）
      + 排查指引 + 行为基准指向 probe_state 测试
- [ ] 事实与源码逐条核对（acp_bootstrap.py L44–89 / runtime.py L487–559）

## T3 · 死代码清理（commit 4）

- [ ] 删 `PIP_INSTALL_TIMEOUT` / `ACP_PACKAGE` / `_installed_acp_version` / `_pip_install_into`
- [ ] `deps_missing` hint 内联 `agent-client-protocol` 字面量（输出文案不变）
- [ ] 裁剪 `TestInstalledAcpVersion` / `TestPipInstallInto` 两类
- [ ] `TestEnsureHermesAcp` 5 用例 `_pip_install_into` patch → `subprocess.run` patch
- [ ] 新增 `TestAutoInstallRemoved` 源断言（模块无四符号 + src/ 文本零引用）
- [ ] `test_hermes_acp_bootstrap.py` 全绿

## 收尾验收

- [ ] `grep -rn "_pip_install_into\|_installed_acp_version\|PIP_INSTALL_TIMEOUT\|ACP_PACKAGE" src/` 零命中
- [ ] 两 1982 文件 + hermes acp bootstrap 全绿（最终态复跑）
- [ ] 自审查 5 轴 + 开源前置增量检查（黑名单拦截 / 敏感增量扫描 / commit message 全英文）
- [ ] push `feat/issue-2134`，建 PR（base=arch-weekly-11-zj）+ Issue 评论 + A2A QA
