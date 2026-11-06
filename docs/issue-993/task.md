# Issue #993 任务拆解

- [x] M1 分支级写租约：`branch_guard.acquire/renew/release`，lock key=`branch_guard:<repo_key>:<head_branch>`。
- [x] M2 push 前置守卫：PR merged/closed 阻断；远端 head 漂移阻断；查询异常 fail-open。
- [x] M3 合入门禁结构校验：围栏配平（软）、`pr_number: 0` / 元数据矛盾（硬），决策落 `grape_t_merge_gate_log`。
- [x] pre-push 钩子：`repogit.ensure_issue_worktree` 建/复用时安装，覆盖手工 push。
- [x] 服务端自动推送接入：`agent._submit_linked_repo_prs` 与 `event_pipeline._submit_direct_linked_prs` 均先取租约 + preflight + 成功后记录 local sha（--no-verify 避免自锁）。
- [x] PR 状态查询全量翻页并按 head 取最近一条，避免 >100 条时漏 merged 而 fail-open。
- [x] 测试：租约争用、跨 holder 钩子自锁、连续推送不误报漂移、merged 阻断、结构校验范围/误报回归。
