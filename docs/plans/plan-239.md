# Plan: Issue #239

## 任务拆解

| # | 任务 | 文件 | 类型 |
|---|------|------|------|
| 1 | get_pipeline_config 返回增加 display_name | src/api/pipeline_config.py | 后端 |
| 2 | 任务列表 scene_name 改用 display_name 优先 | src/api/issues_center.py | 后端 |
| 3 | Issue pipeline 详情 scene_name 改用 display_name 优先 | src/api/routes/issues.py | 后端 |
| 4 | 创建需求下拉框 s.name → s.display_name \|\| s.name | src/api/static/js/pages/issues.js | 前端 |
| 5 | 复制需求下拉框 s.name → s.display_name \|\| s.name | src/api/static/js/pages/issues.js | 前端 |
| 6 | 新增单元测试覆盖 display_name 优先逻辑 | tests/ | 测试 |
| 7 | 五轴自审查 | — | 质量 |
| 8 | 建 PR + Issue 评论 + A2A QA | — | 流程 |

## 执行顺序
1. 先改后端 pipeline_config.py（基础）→ 2/3 依赖此返回值
2. 改 issues_center.py / issues.py
3. 改前端 issues.js
4. 写测试
5. 自审查 → PR → 评论 → A2A
