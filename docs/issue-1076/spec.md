# Spec: Issue #1076 — issues update 的 milestone 设置可靠化

## Objective
让 `grape git-repo issues update --milestone_id` 可靠：合法标识（`milestones.number`）必然落地并回读一致；非法标识返回可读 4xx；失败请求不清空既有 milestone。

## 现场实证
| 传入 `milestone_id` | 结果 |
|---|---|
| 850012（milestones.number，weekly-03） | 200，回读一致 |
| 3（REST 内部 id） | 502 回读校验失败 |
| 999999（不存在） | 502 回读校验失败 |
| 先设 850012 再传 999999 | 502 后回读为 null（既有 milestone 被清空） |

合法取值口径 = `milestones` 列表的 `number` 字段（非 REST 内部 id）。

## Scope
- 平台 `PATCH /api/git-repo/{owner}/{repo}/issues/{number}`：milestone 映射与回读校验。
- 非法/不存在值 → 422（可读错误含合法取值来源），不再 502。
- 失败原子性：写入前预校验 number 存在；回读不一致时回滚原 milestone。
- 上游真实故障（预校验/回读不可用、网关/连接）仍返回 5xx。
- 文档补充取值来源与错误码语义。

## Out of Scope
- 不支持 REST 内部 id 作为 `milestone_id`。
- 不回溯历史 Issue 的 milestone。
- #1108 纯文档映射条目由该单承接。

## Acceptance Criteria
1. `milestone_id=850012` 设置成功且回读一致。
2. 非法值（3/999999/非数字）返回 4xx + 可读错误，无 502。
3. 失败后既有 milestone 保持原值。
4. 上游真实故障仍返回 5xx。
5. 文档标注取值来源，测试覆盖以上场景。