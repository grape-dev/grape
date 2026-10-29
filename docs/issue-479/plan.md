# Plan: Issue #479 流水线页场景列真实场景名

## 方案
1. 前端 `src/api/static/js/pages/pipeline.js`：删除 `_sceneLabels` 写死映射表，
   场景列取值改为 `p.scene_name || _sceneId`（后端真实名优先，缺失兜底原始 ID）。
   后端三处接口（routes/pipeline.py、issues_center.py、routes/issues.py）已按
   (scene_id, repo_key) 解析 display_name，零改动。
2. Locale 清理：locales/{zh,en}/pipeline.json 删除失去引用的 7 条 scene_* 文案；
   tasks/dashboard 的同名键仍有引用，保留。
3. 测试：tests/test_issue_479_scene_label.py 双层——
   - 源码检查层（沿用 #372 tests/test_issue_372_round3_frontend.py 模式）：写死表已删、
     取值顺序正确、locale 清理到位、其他页面键未误删。
   - 行为契约层（UC7，沿用 #239 tests/test_issue_239_scene_display_name.py 的 DB 种子模式）：
     种改名场景 + events 行 → 直接调 get_pipeline 路由（session_token 作用域跳过
     仓库权限校验）→ 断言响应 scene_name == 改名后的 display_name。

## 测试 seam
- 前端源码断言（静态检查，CI 可跑）。
- 后端行为断言：get_pipeline 路由函数级调用，不依赖登录态与网络。

## 任务拆解（TDD）
1. test: tests/test_issue_479_scene_label.py 源码检查用例（红：修复前写死表存在、
   scene_name 被覆盖）。
2. feat: pipeline.js 删表 + 取值顺序调整（绿）。
3. test: locale 清理断言 + 回归保障（tasks.scene_default_badge 未误删）。
4. feat: 清理 locales/{zh,en}/pipeline.json 7 条 scene_* 文案。
5. test: UC7 行为契约用例（TestBackendSceneNameContract 升级为真实调用
   get_pipeline，种 DB 数据验证改名场景的 scene_name）。
6. 回归：test_issue_239_scene_display_name.py + node --check + locale JSON 解析。
7. 文档：docs/issue-479/{spec,plan,task}.md（场景盘点 S1-S7、用例 UC1-UC7）。
