# Spec: Issue #1731 — GRAPE_SERVER_URL 指向本机 runtime 网关导致业务端点全 404

> 目标分支：weekly-mys（工作基线，以 `$GRAPE_WORK_BRANCH` 为准）

## Objective
- **要构建什么**：修正任务注入基址的解析链路，使本机 runtime 场景下注入的 `GRAPE_SERVER_URL` 一定指向可用的业务 API，而不是 runtime 网关端口。
- **为什么**：基址错配让 `issues` / `knowledge` / `repos project info` 全量 404，阻断每个 Agent 的首轮业务读取，造成全链路空跑与逐条命令试错；已有多次同源复发。
- **用户**：平台各 Agent 任务链路（VOD / Issue / Coding / SRE）及运维。
- **成功标准**：单机多进程（runtime 8083、业务 API 8081）下，注入的基址首次调用业务命令即成功，无需 `--base-url` 覆盖。

## Scope
### In Scope
- `src/prompt_builder.py::_resolve_business_server_url`：本机 runtime 分支返回回环地址前增加业务端点可用性探测，探测失败即跳过该候选并回退 `server_base_url`。
- 端口来源解耦：业务端口 / 业务 base-url 由显式配置承载，`_local_port()` 仅作兼容默认。
- 注入结果可审计：记录最终基址与命中来源分支（不含 token）。

### Out of Scope
- 技能侧 base-url 探测兜底与语义文档（属 grape-skills 仓，另行跟踪）。
- runtime 网关自身端口与路由调整；`RUNTIME_SERVER_URL` 语义保持不变。

## 验收标准
- [x] 本机 runtime（8083）与业务 API（8081）分离时，注入的 `GRAPE_SERVER_URL` 指向业务 API。
- [x] 探测失败的候选被跳过并回退下一优先级，回退过程有可审计日志（不含 token）。
- [x] 显式 `task_business_api_url` 仍最高优先级；远程 runtime 与单地址部署行为与改造前等价。
- [x] 回归用例覆盖「本机 runtime + 非业务端口」的地址解析结果。
- [x] 相关现有测试全部通过。