# org-repo-analyzer 结果字段（schema v3）

> 展示顺序：`purpose` → `core_functions` → `core_responsibilities` → `other_capabilities`。
> 路由顺序：`task_examples` / `not_for` / `owned_entities` / `routing` 决定「任务派给谁」。
> 接口、依赖、文件路径只用于机器召回与审计，不进入人类可读摘要正文。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| schema_version | int | 是 | 固定 3 |
| repo_key | string | 是 | provider:owner/repo |
| full_name | string | 是 | owner/repo |
| source_commit | string | 否 | 分析时的提交（来自上下文） |
| purpose | string | 是 | 一句话定位（组织视角：为什么存在、服务谁） |
| core_functions | object[] | 是 | 3–5 项核心功能，按业务重要性排序 |
| core_functions[].name | string | 是 | 业务能力名，不是模块/文件名 |
| core_functions[].description | string | 否 | 完成什么业务动作、产生什么结果 |
| core_functions[].keywords | string[] | 否 | 该能力的召回关键词 |
| core_functions[].task_examples | string[] | 是 | 3–5 条应派到本能力的真实任务 |
| core_functions[].not_for | string[] | 否 | 2–3 条相似但不该派到本能力的任务 |
| core_functions[].owned_entities | string[] | 否 | 本能力拥有的业务对象/数据/主题 |
| core_functions[].change_types | string[] | 否 | api/database/business_logic/ui/infra/test/docs |
| core_responsibilities | object | 是 | 核心职责与边界 |
| core_responsibilities.role | string | 否 | 组织角色与上下游关系 |
| core_responsibilities.owns | string[] | 是 | 明确由本仓库负责的职责 |
| core_responsibilities.not_owns | string[] | 否 | 明确不负责、由其他仓库承担的能力 |
| routing | object | 否 | 仓库级路由判别器 |
| routing.include_keywords | string[] | 否 | 能区分本仓的正向词 |
| routing.exclude_keywords | string[] | 否 | 最容易误派到本仓的排除词 |
| routing.typical_requests | string[] | 否 | 仓库级典型任务 |
| other_capabilities | string[] | 否 | 其他能力的概括名称（≤10），不展开 |
| domains | string[] | 是 | 业务/技术领域 |
| keywords | string[] | 是 | 召回关键词（≤30） |
| interfaces | string[] | 否 | 对外 HTTP/CLI/消息接口（仅机器使用） |
| depends_on | string[] | 否 | 依赖仓库 owner/repo（仅机器使用） |
| technical_details | object | 否 | 技术栈/关键路径等审计信息（不展示） |
| confidence | float | 是 | 0–1 |
| evidence | string[] | 是 | 依据的文件/上下文（仅审计） |
| risks | string[] | 否 | 待人工确认项 |

兼容：v1/v2 结果仍可保存；后端会派生 `core_functions`、`core_responsibilities`、
`other_capabilities`，并从能力名/关键词生成基础 `routing`。

写入后由后端渲染为 `org-context/repo-map/repos/<provider>/<owner>/<repo>/summary.md`，
并同步到组织仓库的 `grape-org` 分支。