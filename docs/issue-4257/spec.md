# Issue #4257 Spec：PR 评论一致性守卫区分链路顺序引用与正文主语

> 来源：Issue #4257 Spec（由 issue Agent 发布） | 基线分支：weekly-mys

## 目标
PR 评论一致性守卫不得把「链路顺序」中的 `issue#<seq>`（A2A 事件序号引用，如
`vod#25105 → issue#25107 → coding#25108`）误判为正文主语引用其它 Issue，消除 409 误拦截。

## 现状
守卫 `first_issue_anchor` 使用 `(?i)(?<![a-z])issues?\s*#?\s*(\d+)` 匹配正文主语，
`\s*#?` 允许 `issue` 与编号零空白紧邻，因此 `issue#25107` 被字面命中并判为其它 Issue，
评论写入返回 409；改写为 `issue(25107)` 可绕过（workaround）。

## 期望
- 仅显式主语形态 `Issue #N` / `Issue N`（大小写不敏感，Issue 与编号间有空白）参与断言；
- `段名#序号` 链路引用形态（`vod#N` / `issue#N` / `coding#N` / `qa#N` 等，含 `→` 连续链）
  不参与主语判定；
- 真正引用其它 Issue 的 409 拦截语义不放宽。

## 验收
1. 正文含链路顺序引用（`vod#25105 → issue#25107 → coding#25108`）的 PR 评论正常写入，不 409。
2. 正文主语确为其它 Issue（`Issue #N` 明确形态）时仍 409。
3. 主语与当前任务 Issue 一致的既有行为回归不变。
4. 现有守卫测试全绿，新增用例覆盖上述三种场景。

## 范围外
- 不放宽真跨 Issue 拦截；
- 不调整 #3811 所述 release guard 读库路由 fail-open 问题；
- 不改评论 API 其它校验规则。