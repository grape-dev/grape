# Tasks: Issue #1731

- [x] `src/prompt_builder.py`：新增业务端点探测（health / openapi）、进程内缓存与超时配置。
- [x] `src/prompt_builder.py`：`_resolve_business_server_url` 本机分支候选链探测与回退。
- [x] `src/prompt_builder.py`：`resolve_task_server_env` 注入审计日志（地址 + 来源，不含 token）。
- [x] `src/user/db.py`：新增 `get_business_api_port`（`task_business_api_port` > `GRAPE_BUSINESS_PORT`）。
- [x] `configs/server-config.example.json`：补充 `task_business_api_port` 示例与说明。
- [x] `tests/test_issue_1731_business_api_probe.py`：覆盖本机 runtime + 非业务端口、回退、显式优先、探测缓存与审计日志。
- [x] 回归：更新 #1577 用例适配探测（本机候选可达时仍返回回环地址）。
- [x] QA 复审修复：探测缓存加条数上限与淘汰；`_business_server_url_source` 改为读取解析决策（消除优先级复制），来源不再失真（`unreachable_fallback`）。
- [x] QA 复审修复：审计日志地址脱敏（userinfo/query），新增脱敏用例。
- [x] QA 复审修复：locality 未知分支先探测本机候选；补单地址本机等价用例。
- [x] QA 复审修复：`get_business_api_port` 复用 `_split_url_configured_value`（含 int 值），`.env.example` 记录 `GRAPE_BUSINESS_PORT` 与探测超时/TTL。
- [x] 运行相关测试：`#1731` / `#1577` / `prompt_builder` / `#1109` / `#372` / `#471` / `#1660` 等通过。