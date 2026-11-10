# Issue #1858 任务拆解

- [x] 配置层：`task_vcs_api_url` / `task_issue_api_url` / `task_a2a_api_url` /
      `task_gateway_base_url` 读取与 server-config 双写。
- [x] 解析层：三面基址解析、网关前缀路由、来源图 `GRAPE_BASE_URL_SOURCES`。
- [x] 注入层：事件派发/重派/对话入队/direct 执行/worker env 重拉统一注入。
- [x] 兼容层：本机/内网 worker 覆盖、旧快照缺失键兜底。
- [x] A2A 自调用：重派与恢复续办改用 A2A 基址，失败附候选 base_url。
- [x] 诊断：`format_base_url_candidates` 输出候选地址与非 0 退出码提示。
- [x] 回归测试：三面注入、网关前缀、来源分离、本机覆盖、失败提示、worker env 重拉。
- [ ] QA 审查（A2A 交办）。
