# Issue #995 任务拆解

- [x] issue 表幂等迁移 + 新库 DDL（vcs_exists/vcs_checked_at）
- [x] 新增 src/api/issue_vcs_index.py（provider 索引 + TTL + 回写 + 回填）
- [x] _query_tasks 返回 VCS 存在性字段
- [x] list_repo_issues 默认过滤 / include_deleted / 字段标注 / 分页口径
- [x] main/grape_server 启动异步回填
- [x] OpenAPI 描述与 API 文档更新
- [x] 新增测试：默认过滤、include_deleted、分页 total、fail-open、跨 provider、回填幂等
