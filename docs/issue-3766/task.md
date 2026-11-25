# Task Breakdown: Issue #3766

## T1 探测原语与 `/api/issues/mine` 判据
- [x] 新增 `_http_json_status`（状态码感知，不抛异常）
- [x] 新增 `_probe_business_issues`（2xx JSON / 401/403 鉴权挑战 = 可用；301→404/HTML = 不可用）
- [x] `_probe_business_api` 增加 issues 第三级兜底
- 验收：探测单测覆盖可用/不可用两类响应

## T2 候选链与注入校验
- [x] `_business_candidate_chain`：显式 → 本机业务端口 → `server_base_url` → 显式 `worker_connect_url`，去重
- [x] `_resolve_business_base` 对显式与远程 runtime 一并校验
- [x] 远程 runtime 排除本机回环候选
- 验收：admin 前端显式配置回退到可用业务基址；远程不落 127.0.0.1

## T3 守护模式与告警
- [x] `GRAPE_BUSINESS_API_GUARD=warn|strict|off`
- [x] 无可用候选：`warn` error 告警并回退运维声明值；`strict` 抛 `BusinessApiGuardError`；`off` 旧行为
- [x] `get_url_split_warning` 覆盖 `task_business_api_url` 回环告警
- 验收：三种模式单测覆盖，告警日志含修复指引

## T4 配置与文档
- [x] `configs/server-config.example.json` 增加 `_comment_issue_3766`
- [x] `help/usage/agent-environment-variables.md` 增加注入守护说明
- [x] `docs/issue-3766/{spec,plan,task}.md`

## T5 回归
- [x] 新增 `tests/kernel/test_issue_3766_business_api_injection_guard.py`
- [x] 更新 #1731 两条断言（显式/远程从「不探测」改为「校验后仍优先」）
- [x] 相关既有测试（1731/1577/1858）通过

## 遗留（不在本单范围）
- 生产 nginx 实际路由下发与单任务环境冒烟（部署侧执行）
- 历史同因反馈单逐单处理
- 技能侧 `--base-url` 自救逻辑增强（grape-skills）