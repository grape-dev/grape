# Task: Issue #461 — 任务拆解

- [ ] T1 db_setup：devops_pipeline_scene 幂等补 deploy_url 列
- [ ] T2 scenes.py：GET/PUT /{scene_id}/deploy-url 端点（校验+归属+404）
- [ ] T3 dashboard.js：复制场景按钮前地球仪图标 + 三态渲染 + XSS 转义 + 校验 + 加载
- [ ] T4 i18n zh/en + index.html 缓存版本 bump
- [ ] T5 help/api/06-scenes.md 文档 + test_openapi.py 契约登记
- [ ] T6 测试 test_issue_461_scene_deploy_url.py / .js（RED→GREEN）
- [ ] T7 回归：js 测试 + pytest 相关套件
