# Issue #672 — 404/403 降级指引（runbook 补充）

适用端点：`issues pending-review / review-pass / review-reject / awaiting-input`（`--issue_id`）与
`issues detail`（`--owner/--repo/--number`）。

## 现象与处置

| 现象 | 根因 | 处置 |
|------|------|------|
| `pending-review --issue_id N` 返回 404 需求不存在 | 内部需求库无主键 N 行（注入的 GRAPE_ISSUE_ID 为远端未建档编号） | 服务端已内建兜底：N 会被视作 GitCode number 在会话仓库作用域内二次解析；远端存在时自动建最小行。仍 404 → 该编号在远端亦不存在，人工核对 |
| `--issue_id N` 返回 403 仓库作用域不匹配 | N 是 GitCode number，撞上其它仓库的内部 issue.id | 同上兜底：命中会话仓库 number 行则放行；未命中且远端无此编号才保持 403 |
| `issues detail --owner O --repo R --number N` 404，但 `git-repo issues get` 同编号 200 | 本地 stage/status/events 聚合无该编号（未建档/未产生事件） | 服务端已内建 VCS 兜底（`source: "vcs"`）；仍 404 → 编号在远端不存在 |
| VCS 兜底返回 502（token 解析失败） | 仓库未配置 bot token / 用户 token 缺失 | 在仓库托管配置中补齐 token 后重试 |

## Agent 侧建议顺序
1. 优先用 `issues detail` 取 `issue_id`（内部主键）调用 pending-review 系列；
2. 仅注入了 `GRAPE_ISSUE_ID`/number 时直接传原值，双路径解析自动兜底；
3. 兜底后仍 404/403/502 → 在 Issue 评论说明失败模式（端点/入参/返回码），转人工核对远端与托管配置。

## 与 #667 关系
同属「detail 404 族」：#667 为新 Issue detail 404，#672 为双路径解析/作用域。本修复仅覆盖 #672 范围，两案互引不合并。
