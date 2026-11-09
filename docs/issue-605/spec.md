# Issue #605 Spec（引用）

既有 Spec 由 issue agent 发布于 Issue #605 评论 197313988（评级 ready），完整有效，本目录不重复生成。
目标分支按跨流水线移交裁定为 `weekly-08-mys`（评论 197469428）。

摘要（详见原 Spec）：
- 目标：定位并修复 hermes 引擎（rid 17004）持续 0 派发的服务端根因，补 `engine_select` 派发决策可观测性。
- 范围：服务端 `engine_select`/`pick_engine`/`claim_by_tag` 派发链路；worker 侧已排除（Issue 正文 7 项）。
- 关键点：取证优先（17004 库内字段、候选列表与过滤原因、任务 runtime_id 绑定）→ 三选一修复 → 结构化决策日志 → 回归验证 agent:vod 批次。
