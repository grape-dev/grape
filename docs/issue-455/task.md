# Issue #455 任务拆解（Task）

> 里程碑：M1（P1）→ M2（P2）→ M3（P3）。每步含测试后原子提交。

## M1（P1）

- [ ] T1.1 README/README.zh/requirements/doctor：Python ≥3.10 声明
- [ ] T1.2 src/api/run.py + main.py + start_server.py：启动版本检查（<3.10 显式提示退出）
- [ ] T1.3 src/user/db.py：server_base_url 配置判定辅助
- [ ] T1.4 src/api/main.py lifespan：基址未配置启动告警
- [ ] T1.5 src/api/routes/health.py：/api/health 增加基址配置状态字段
- [ ] T1.6 README：server_base_url 配置文档 + oauth-providers.json 凭据文档（双 provider 示例）
- [ ] T1.7 src/api/db_setup.py：补建 pipeline_instances 表
- [ ] T1.8 src/api/onboarding.py：清理前表存在性检查降级
- [ ] T1.9 src/api/routes/grape_auth.py：OAuth 自动创建读回校验（账号+绑定，失败 ERROR + 失败响应）
- [ ] T1.10 测试：M1 相关新增测试通过 + 现有测试回归

## M2（P2）

- [ ] T2.1 src/api/routes/repos.py：host_repo 502 detail 补充上游上下文（stage/error_code/retryable）
- [ ] T2.2 src/api/routes/repos.py：host_repo GitHub 分支归属/权限前置校验 → 403 PERMISSION_DENIED
- [ ] T2.3 前端 repos.js + locales：403/502 error_code → i18n toast 明确提示
- [ ] T2.4 测试：M2 相关新增测试

## M3（P3）

- [ ] T3.1 src/runtime/repogit.py：_run_git_retry 有限次退避重试（网络类），fetch/clone 使用
- [ ] T3.2 src/worker_package.py：clone 网络类重试
- [ ] T3.3 docs/0-to-1-checklist.md：自检清单文档 + README 链接
- [ ] T3.4 README：镜像/代理建议补充
- [ ] T3.5 测试：M3 相关新增测试

## 收尾

- [ ] T4.1 全量回归（相关模块 pytest 通过）
- [ ] T4.2 自审查（5 轴）
- [ ] T4.3 创建 PR → Issue 评论 → A2A QA