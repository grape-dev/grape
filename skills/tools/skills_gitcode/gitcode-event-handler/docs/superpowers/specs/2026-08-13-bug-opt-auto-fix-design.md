# 设计文档：bug/优化类 issue 一律直接进机器人修复流水线

日期: 2026-08-13
状态: 已批准（用户 clarify 选择"bug + 优化类都改成直接进机器人"）
目标脚本: /root/.hermes/skills/huawei-cloud/gitcode-event-handler/scripts/gitcode_event_handler.py
部署入口: /root/.hermes/config.yaml 190 行 webhook 路由 prompt

## 功能定位

新增 bug 类型 issue 时，**无论当前 skill 是否人工开发**，都直接进入机器人修复
流水线（建 Multica 任务 @skills_dev → 入库 → 打 bug,triaged → 评论带 devflow
链接）。不再按"贡献者是否含机器人账号"分流到 codeowner/艾特历史提交人。
优化类需求（2.1.3）同步修改：非机器人 skill 的优化也直接进机器人开发流水线。

## 现状（改动前）

handle_issue 创建路径 + handle_issue_update 升级路径，bug 分流 2.2.2 ② 与
优化类 2.1.3 均含"判断 skill 贡献者是否含机器人账号"（_skill_has_bot_contributor）：
- 含机器人 → _bug_is_bot=True 自动修复 / 优化直接开发
- 不含机器人 → 质量检查（仅 bug 非 bot 前置）→ 打 {label},triaged →
  codeowner_assign_issue（codeowner 映射 → 提交历史推断 _skill_primary_contributor
  → 兜底跳过）→ 不入库（2026-08-12 "bug 按需入库"拍板）

## 改动点（4 处）

| # | 位置 | 函数 | 改动 |
|---|------|------|------|
| A | 4673-4707 | handle_issue bug 分流 | else 分支（贡献者判断+非bot codeowner 块）→ 直接 _bug_is_bot=True |
| B | 4940-4961 | handle_issue 优化类 | _opt_is_bot 判断 + 非bot codeowner 块删除 → 继续向下 |
| C | 5221-5247 | handle_issue_update bug 分流 | 同 A |
| D | 5271-5296 | handle_issue_update 优化类 | 同 B |

## 保留不变

- 2.2.1 skill 名提取/格式校验（无/不完整 → 提示修改，不入库）
- 2.2.2 ① 仓库存在性检查（无此 skill → 提示改为需求；限流宽松命中 → 提示完整名）
- 主路径质量检查（4859，#246 拍板"质量检查一律生效"：bug 缺复现步骤/日志 → bug,need-info 追问）
- 华为云相关性（无关 → invalid 关闭）、判重、竞品云、已存在检查跳过条件
  （_bug_is_bot=True 跳过"仓库已有同名 skill"误关）
- codeowner_assign_issue 函数：仅剩通用 codeowner 分支（4717，标题无 skill
  关键词的非 bug/非优化 issue）一个调用点；_skill_has_bot_contributor /
  _skill_primary_contributor 函数保留（无调用点但被测试引用，不删）
- 转机器人评论（is_bot_fix_request，req=None 补建）作为历史 issue 入口保留

## 行为变化

- 人工开发 skill 的 bug：不再艾特提交人/模块负责人，直接建 Multica 任务自动修复
  （必然入库 dispatched + flow done，取代 2026-08-12 "bug 按需入库"中 codeowner
  路径不入库的语义；"按需入库"仅剩"艾特转机器人补建"入口）
- 人工开发 skill 的优化类需求：直接进机器人开发流水线
- update 事件（用户编辑补充）重新评估时行为与 create 一致

## 测试计划

- 新增 test_bug_opt_auto_fix.py：bug+人工skill→action=created 且 codeowner/
  _skill_has_bot_contributor 不被调用；优化类+人工skill→同断言；update 路径同断言
- 全量回归：tests/ 下 14 个文件（函数级测试不受影响）

## 风险边界

- 人工 skill 的 bug 自动修复依赖机器人能力，可能修不了需人工兜底（用户可评论
  @机器人转人工或自行处理，现状无人工兜底通道，记录在案）
- 429 限流下仓库存在性无法确认 → 降级直接修复（宁自动不误关，原语义）
