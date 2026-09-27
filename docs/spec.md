# Spec: Issue #242 — 支持跨仓库需求开发（副本）

## Objective

在场景详情中增加「关联仓库」配置项（仓库 + 分支多选），支持一次需求开发同时操作多个仓库，开发完成后向每个有改动的仓库分别提交 PR。

- **用户**：使用 Grape 平台进行多仓库项目开发的开发者/团队
- **成功标准**：场景详情可配置多个关联仓库及分支；一次需求开发可同时操作多个仓库；开发完成后能向每个有改动的仓库分别提交 PR

## Tech Stack

- Python 3.12 + FastAPI（后端 API）
- SQLite/MySQL（devops_pipeline_scene 表）
- 原生 JS（前端，无框架）
- grape CLI（VCS 操作）

## Commands

```bash
# 测试
pytest tests/ -x -q

# 启动（验证用）
python -m src.api.run  # 或 uvicorn
```

## Project Structure

```
src/api/db_setup.py              # DB schema 迁移
src/api/pipeline_config.py       # 场景 CRUD 数据层
src/api/routes/scenes.py         # 场景 API 路由
src/api/routes/webhook.py        # A2A dispatch
src/api/event_pipeline.py        # env_extra 注入
src/runtime/repogit.py           # git worktree 基建
src/runtime/agent.py             # coding agent 执行
src/vcs/api_client.py            # VCS PR 操作
src/api/static/js/pages/dashboard.js  # 前端场景详情页
tests/                           # 测试
```

## Code Style

- 后端：Python type hints，现有 `_ensure_column` 幂等迁移模式
- 前端：原生 JS，Apple 设计风格（rounded-lg, 0.5px border, SF Pro）
- DB：新增字段用 `_ensure_column` 幂等添加，TEXT DEFAULT '[]'

## Testing Strategy

- pytest 单元测试：场景 CRUD 含 linked_repos、A2A payload 携带 linked_repos
- 测试位置：tests/test_linked_repos.py

## Boundaries

- Always: 运行测试后提交、幂等迁移、向后兼容
- Ask first: 无
- Never: 修改非 Issue 相关代码、提交 secrets

## Success Criteria

- [ ] 场景详情提供关联仓库配置（仓库 + 分支，支持多选）
- [ ] 一次需求开发可同时操作多个关联仓库（各仓库独立 worktree）
- [ ] 开发完成后能向每个有改动的仓库分别提交 PR
- [ ] 方案包含跨仓库协同开发的技术设计
- [ ] 所有现有测试通过 + 新增测试覆盖

## Open Questions

- 无（Spec 已明确）
