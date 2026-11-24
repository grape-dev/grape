# Issue #3731 需求文档：feedback→Issue 转换全线失效（路由解包崩溃 + 标题超 200 未截断）

> 目标合入分支：`arch-weekly-11-ljt` | 来源：Issue #3731 Spec 评论（issue agent，2026-11-22）

## Objective

- 恢复「反馈 → Issue」自动建单主链路：`skill.scope=repo/user` 的 Agent 反馈与带 `repo_key` 的用户反馈能正常建单并回写 `user_feedback.issue_url`。
- 消除 7 天 20.7 万条 WARNING 噪声与超长标题 400 永久卡死：故障可被 admin 感知（连续失败升级 ERROR + 计数落库可查）。
- 用户：提交反馈的 Agent / 用户 + 运维 admin。成功标准：`target resolve failed` 归零、`out of max length` 归零、对应反馈 `converted>=1`。

## 现象与根因（当前基线实读）

1. `src/api/feedback_issue_converter.py:77` `_repo_target()` 与 `:85` `_repo_bot_token()` 仍是
   `provider, _ = parse_repo_key(repo_key)` 的 **2 元解包**；而 `src/kernel/repo_key.py:34`
   `parse_repo_key()` 返回 `(provider, owner, repo)` **三元组**。
   → 每次进入 `_resolve_target()` 的仓库/用户反馈分支即抛
   `ValueError: too many values to unpack (expected 2)`，被 `convert_pending_feedback()`
   （:242）的 `except Exception` 吞成 WARNING、整行跳过并无限重放（50 个 fid / 1h 4679 次）。
2. `_create_provider_issue()`（:92）把 `title` 原样投给平台，未按 200 字符上限截断；
   超限 400 被 :272 捕获后只记 WARNING + `_release()` 回待转换态 → 同一 fid 每轮必败（fid=328 永久卡死）。
3. `_find_existing_open_issue()`（:164）以 `_norm_title(title)` 原串做查重键，与建单侧口径不统一；
   一旦建单侧截断，截断后的长标题反馈会重复建单。
4. 失败不可观测：路由/建单失败只有每 tick 一条 WARNING，没有累计轮次，也没有落库状态供 admin 查看。

## 范围

### In Scope

1. 路由解析收敛到 `parse_repo_key()` 三元组契约（`_repo_target()` / `_repo_bot_token()`）。
2. 新增 `_MAX_TITLE_LEN = 200` + `_clip_title()`：建单前截断（保留尾部省略号，`len <= 200`），
   查重键使用同一截断函数，保证建单/查重同键。
3. 失败可观测：`user_feedback.convert_fail_count` 持久化「连续失败轮次」（跨 worker 共享库可见），
   达到阈值（默认 3，`FEEDBACK_CONVERT_FAIL_ESCALATE` 可调）升级为 `log.error`；建单成功后清零。
4. 单测覆盖：三元组解包三态（带前缀 / 裸键 / 空键）、超长标题截断 + 查重幂等、连续失败升级与成功清零。

### Out of Scope

- 不改 `_official_skills_target()` 的官方技能仓路由语义与 `GRAPE_FEEDBACK_OFFICIAL_ISSUE` 开关。
- 不改 reconcile 调度周期、topic 端口、事件流与前端「我的反馈」视图。
- 不做表结构重构（仅新增一个计数列，见 plan.md 决策记录）。

## 验收标准

- [ ] `grep -n "parse_repo_key" src/api/feedback_issue_converter.py` 无 2 元解包残留；全仓调用点均为 3 元解包或下标取值。
- [ ] repo 级 / user 源反馈各 1 条 → `convert_pending_feedback()` `converted>=1`，`user_feedback.issue_url` 回写。
- [ ] 350 字符标题反馈可建单成功，投递标题 `<=200` 字符；`out of max length` 归零。
- [ ] 截断后同键的重复反馈复用已有单号并回写（不重复建单）。
- [ ] 同一 fid 连续 3 轮失败日志为 ERROR 且 `convert_fail_count` 可查；成功后计数清零。
- [ ] 现有测试 + 新增测试全通过。