# Issue #672 方案设计与任务拆解

## 方案（Spec 决策 B：number+owner/repo 兜底，含必要同步修复）

### 改动 1：`_session_devops_issue` 双路径解析（src/api/routes/issues.py）
pending-review / review-pass / review-reject / awaiting-input 共用的鉴权解析函数：
1. 原路径：按内部主键 `issue.id` 查行；scope 匹配 → 返回（不变）。
2. 兜底路径（Issue #672）：主键 miss（404）或 scope 不匹配（403）时，把入参视作 GitCode number，
   在**会话 token 仓库作用域**内按 `repo_key + issue_number` 二次解析；命中 → 返回该行。
3. 末级兜底：本地亦无行时，经 git-repo 通道（number+owner/repo，bot token）核对远端存在；
   存在 → `_sync_issue_fields(ensure_row=True)` 建最小行后返回（同步修复，覆盖存量缺失行）。
4. 全 miss → 维持原 404/403（文案补充双路径说明）。

### 改动 2：`get_issue_detail` VCS 兜底
本地 stage/status/events 三 map 均无该编号时，不再直接 404：先按 number+owner/repo 走
git-repo 通道核对（gitcode→github 双试）；存在 → 返回以 VCS 字段为基的最小 detail
（`source: "vcs"`、空 events/pipeline）；不存在 → 404（原语义）。

### 改动 3：runbook 降级指引（docs/issue-672/runbook-fallback.md）
404/403 时优先用 GitCode number 重试（兜底路径已内建），仍失败则降级评论说明。

## 任务拆解
1. [x] 复现三组失败判定分支定位（_session_devops_issue / get_issue_detail）
2. [x] 数据排查结论：issue 表缺行（无建档链路触达），作用域过滤在 id 撞号时误判
3. [x] `_session_devops_issue` number 兜底 + ensure_row
4. [x] `get_issue_detail` VCS 兜底
5. [x] 回归测试 tests/test_issue_672_dual_path_resolution.py
6. [x] runbook 降级指引文档
