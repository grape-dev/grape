# Issue #1858 需求文档

## 背景
默认 `GRAPE_SERVER_URL` 是 runtime 网关（如 8083），而 VCS/Issue 业务 API 在 8081、
A2A 控制面在 8082。同一 base_url 无法同时服务三面，导致单次任务内 `git-repo`/`issues`
返回 404，`a2a handoff` 返回 405、`a2a status` 返回 404；Agent 需逐端口探测或手工
`--base-url` 才能继续。

#1831 / PR #1133 只保证「存在一个业务基址」，本单补齐「按服务路由」的增量缺口。

## 目标
- 平台任务环境默认按服务注入基址，任务 env 中显式可见、来源可追溯；
- 可选按前缀路由的聚合网关（`/vcs`、`/issue`、`/a2a`），作为默认分服务变量的替代；
- 平台侧 A2A 自调用（重派/恢复续办）改用 A2A 基址；
- 404/405 诊断信息包含候选 base_url，并声明非 0 退出码；
- 历史任务 / 旧快照 / 旁路派发路径同样生效。

## 范围
### In Scope
- 任务 env 三面基址注入（事件派发、重派、对话入队、direct 执行、worker 执行前 env 重拉）；
- 本机/内网 worker 基址覆盖；
- A2A 派发基址选择与候选地址诊断文案；
- runbook 前置检查所需的三面基址可见性与缺失告警。

### Out of Scope
- #1831 / PR #1133 已覆盖的「保证存在一个业务基址」；
- grape-cli #151 / PR #20 的 CLI 侧 a2a 指引与 fallback 实现；
- `grape git push` pre-push `distributed_locks` 缺表（#1552 / #1402）。

## 验收标准
- [ ] 单次任务内 `git-repo`、`issues`、`a2a handoff`、`a2a status` 全部可达，无需手工 `--base-url`；
- [ ] 任务 env 含 `GRAPE_VCS_BASE_URL` / `GRAPE_ISSUE_BASE_URL` / `GRAPE_A2A_BASE_URL`，
      且 `GRAPE_BASE_URL_SOURCES` 记录各自来源，与 `GRAPE_SERVER_URL` 分离；
- [ ] base_url 不可达或端点 404/405 时，诊断信息包含候选 base_url，退出码非 0；
- [ ] 历史任务 / 旧快照 / 旁路路径（direct、worker env 重拉、恢复续办、重派）同效；
- [ ] 回归用例覆盖三面注入与失败提示文案，现有测试全过。