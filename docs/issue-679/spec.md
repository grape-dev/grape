# Issue #679 Spec：去掉 Issue 里「[VOD Agent] 任务已完成」占位评论

> 采纳 VOD 在 Issue #679 发布的结构化 Spec（原文见 Issue 评论），本文件为仓库内归档副本。

## 目标

消除 Issue 中无信息量的占位评论（如「[VOD Agent] 任务已完成。」）：agent 产出无法提取出可发正文（conclusion 与纯文本 result_text 均为空）时，平台不再发占位文案，直接跳过回帖。

## 现状（改动基线）

三处发帖入口在「无内容可发」时均硬编码兜底文案并照发：

1. `src/api/event_pipeline.py` issue 回帖主路径（`_post_result_to_issue`）：`_conclusion` 提取失败 → 硬写「任务已完成。」
2. `src/api/event_pipeline.py` PR 订阅回帖（`_post_result_to_pr`）：→ 硬写「PR 审查任务已完成（xxx agent）。」
3. `src/runtime/service.py`（`complete_task`）：`_result_text or "任务已完成。"` 兜底

## 期望

- 三处均改为「无 conclusion 且无有效正文 → 不发评论」（跳过该发帖分支），`<!-- grape-auto-comment -->` 标记逻辑与「agent 已发过评论则跳过」防重复逻辑保持不动。
- `service.py:2018` 附近「任务已完成：级联收敛（回报丢失补偿）」属内部补偿状态描述、不贴 Issue，不动。
- 同步修正依赖「兜底文案必发」的断言（实际位于 `tests/test_issue_451_placeholder_comment.py`；`tests/test_issue_372_deep_analysis.py` 经核对不含此类断言），改为断言「不发」。

## 验收

1. agent 产出含结构化 conclusion → 评论正常发布（行为不变）。
2. agent 产出为纯文本正文（非 JSON）→ 评论正常发布（行为不变）。
3. agent 产出为空串或纯 JSON（无 conclusion）→ 无评论产生，无「任务已完成。」占位评论。
4. PR 订阅回帖路径同 1-3；runtime handler 链路同 1-3。
5. `<!-- grape-auto-comment -->` 标记、已发评论防重复跳过逻辑回归通过。
6. 相关测试全量通过（基线既有失败项除外，见 plan.md 记录）。
