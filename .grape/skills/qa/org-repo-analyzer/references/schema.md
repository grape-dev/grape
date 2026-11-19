# org-repo-analyzer 结果字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| schema_version | int | 是 | 固定 1 |
| repo_key | string | 是 | provider:owner/repo |
| full_name | string | 是 | owner/repo |
| source_commit | string | 否 | 分析时的提交（来自上下文） |
| purpose | string | 是 | 一句话定位 |
| domains | string[] | 是 | 业务/技术领域 |
| keywords | string[] | 是 | 召回关键词（≤30） |
| interfaces | string[] | 否 | 对外 HTTP/CLI/消息接口 |
| depends_on | string[] | 否 | 依赖仓库 owner/repo |
| not_owns | string[] | 否 | 明确不负责的范围 |
| capabilities | object[] | 否 | {name, description, paths[]} |
| confidence | float | 是 | 0–1 |
| evidence | string[] | 是 | 依据的文件/上下文 |
| risks | string[] | 否 | 待人工确认项 |

写入后由后端渲染为 `org-context/repo-map/repos/<provider>/<owner>/<repo>/summary.md`，
并同步到组织仓库的 `grape-repo-analysis` 分支。
