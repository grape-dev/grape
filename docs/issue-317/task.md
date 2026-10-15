# Tasks: Issue #317

- [x] T1 遗言尾部采集：_run_once 读循环维护 _log_tail（50 行/4KB），exit!=0 且 stderr 空时写入 done.errors
- [x] T2 OOM 启发式：_oom_kill_evidence()（dmesg/journalctl/内核日志文件），-9 退出标记 suspected OOM / SIGKILL 无证据
- [x] T3 异常退出自动重试一次：exit!=0 且 stderr 空且未重试过 → _run_once("")，单一 done，event_active_guard 作用域内幂等
- [x] T4 单测：空 stderr 补采 / OOM 标记（两分支）/ 重试幂等（2 次 Popen、1 个 done、与 session_missing 重试不叠加）
- [x] T5 回归：tests/test_agent_executor.py 全量通过
- [x] T6 提交 feat/issue-317 → 建 PR（base=weekly-06）→ Issue 评论 → A2A QA
