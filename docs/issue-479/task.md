# Tasks: Issue #479

- [x] T1 前端红测试：tests/test_issue_479_scene_label.py 源码检查用例（写死表已删、取值顺序、无残留硬编码标签）
- [x] T2 前端实现：pipeline.js 删除 _sceneLabels，sceneLabel = p.scene_name || _sceneId（commit e56560955）
- [x] T3 Locale 清理：locales/{zh,en}/pipeline.json 删除 7 条 scene_* 文案 + 回归保障用例（tasks.scene_default_badge 未误删）
- [x] T4 行为契约测试：UC7 —— 种改名场景 + events → 调 get_pipeline 路由断言 scene_name == 改名后 display_name；UC8 —— latest_event 携带 scene_id/scene_name + 前端徽章取值契约
- [x] T5 回归：test_issue_239_scene_display_name.py 全量 + test_issue_329_cross_pipeline.py 单独跑全绿（注：329/337 与 239 同进程混跑的 event loop 顺序污染为 weekly-09 既有问题，基线复跑同样失败，非本分支引入）+ node --check + locale JSON 解析通过
- [x] T6 文档：docs/issue-479/{spec,plan,task}.md，场景盘点 S1-S7、验收用例 UC1-UC8、家族扫描结论（跨场景徽章缺陷已在本分支修复）
- [x] T6b 跨场景移交徽章修复：routes/pipeline.py latest_event 透传事件级 scene_id + _scene_label_for_event 解析真实场景名；pipeline.js 徽章 scene_name 优先（webhook/payload/A2A 协议零改动）
- [x] T6c Autopilot 派发弹窗场景下拉（UC9，第三轮全页面扫描发现）：autopilot.js display_name 优先（value 保持场景 ID）+ t3_04/t3_05 用例（源码契约 + get_scenes 行为契约）
- [ ] T7 建 PR 到 weekly-09 + 评论链接 + A2A QA
