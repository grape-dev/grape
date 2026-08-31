# 设计文档：2.1.3 优化类分流排除 bug + extract_skill_names 过滤问题单编号

日期: 2026-08-13
状态: 已批准（用户 clarify 选择"两个都改"）
目标脚本: /root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py
触发案例: issue #113（[huawei-cloud-monitoring-query]基本功评测发现1条问题）

## 问题现象

bug issue #113（评测问题单格式）未触发自动研发，被 2.1.3 优化类分流劫持：
1. classify 判 bug ✓（正文"报错"）
2. bug 分流通过（_bug_is_bot=True，仓库有 skill）
3. 质量检查通过（正文有复现步骤）
4. 走到 2.1.3: has_optimization_intent 命中（正文含"改进/修改/修复"——
   评测问题单"修复建议/预期行为"等常规用语）→ 误判优化类
5. extract_skill_names 提取到 2 个名: huawei-cloud-monitoring-query +
   huawei-cloud-monitoring-query-ISSUE-001（**问题编号被误当 skill 名**）
6. 2.1.3 要求唯一 skill 名 → opt_need_skill_name 拦截

## 根因

- 2.1.3 优化类分流不区分 bug/feature：bug 报告正文天然含优化意图词
- extract_skill_names 正则 `huawei[-_]?cloud-[a-z0-9-]+` 贪婪匹配把
  "xxx-ISSUE-001" 问题编号整体提取为 skill 名

## 改动（3 处）

| # | 位置 | 改动 |
|---|------|------|
| 1 | handle_issue 2.1.3 入口（~4894） | `if has_optimization_intent(...)` → `and label != "bug"` |
| 2 | handle_issue_update 2.1.3 入口（~5183） | 同样加 `and label != "bug"` |
| 3 | extract_skill_names（~6555） | 返回前过滤 `-issue-\d+$` 结尾的匹配（问题单编号） |

## 语义

- bug 报告即使正文含"改进/修复建议"，也走 bug 修复流程，不是优化需求
  （与判重 2.0 bug 语义一致，#241）
- 问题单编号（xxx-ISSUE-001）不是 skill 名

## 测试

- test_extract_skill_names_filters_issue_number: 纯函数, 输入含 ISSUE-001 → 只返回真 skill 名
- test_bug_with_opt_words_skips_opt_split: bug + 优化词命中 + 多疑似 skill 名 → action=created
- 全量回归 97 passed

## 端到端验证

#113 真实 payload 重放: action=created, label=bug, SKI-55 已建并派给
skills_dev(5d575b86), DB dispatched + flow node_source/preprocess done,
GitCode 评论"已识别SKILL开发任务" + bug,triaged 标签。
（webhook 的 opt_need_skill_name 旧评论是修复前发的, 幂等不冲突）
