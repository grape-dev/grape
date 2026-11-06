[Coding Agent] Issue #993 Spec（源自 Issue 评论 198696488，开发基线 weekly-mys）

## 一、可开发性评级：ready

四维结论：需求完整（三条期望 + 验收明确）；技术可行（仓库已有 distributed lock、worktree 推送守卫 `worktree_guard`、合入门禁 `merge_gate` 可复用）；依赖就绪（无新增外部依赖，PR 状态查询走现有 `git-repo pulls` 接口）；范围确定（平台侧可交付，merge-bot 侧采用"可感知/可协调"边界）。目标分支：`weekly-mys`。

## 二、Spec：Issue #993 — PR merged 后 push 无提示 + merge-bot 与 Agent 并发写同分支

### Objective
消除同一 PR head 分支上 merge-bot 与任务 Agent 的并发写冲突，并在 PR 已合并后向 head 分支 push 时给出明确、可执行的提示，避免重复解冲突与修复静默丢失在基线之外。

### Scope
**In Scope**
- M1 分支级写互斥：新增分支写租约（lock key = `repo_key + head_branch`），覆盖平台侧所有 worktree 自动提交/推送路径。
- M2 推送前置守卫：push 前校验「PR 状态」与「远端 head 是否已被他人推进」；已合并/已关闭 → 阻断并提示；远端 head 变化 → 阻断并提示重新 fetch/rebase，禁止盲目二次解冲突。
- M3 结构校验：合入门禁新增重建文档结构扫描（代码围栏配平、`pr_number: 0` 残留、元数据口径矛盾），命中按硬/软分级阻断或告警并写入合入日志。
- 守卫提示需同时出现在 git push 输出与任务日志，保证 Agent 可感知。

**Out of Scope**
- 改造 devdzr-merge-bot 自身代码/调度（如为外部机器人，需其对接平台租约才能强互斥）。
- GitCode 平台侧 merge 能力本身；历史已损坏分支的人工修复。

### Technical Approach
1. `src/runtime/branch_guard.py`（新增）：租约获取/续期/释放 + 推送前置判定，复用 `distributed_locks`/`src/lock.py`。
2. `src/runtime/repogit.py`：worktree 创建/复用时安装 `pre-push` 钩子（调用 branch_guard 判定），覆盖 Agent 手工 push。
3. `src/runtime/agent.py::_submit_linked_repo_prs`：push 前接入同一守卫与租约，失败写出结构化 reason。
4. `src/api/merge_gate.py`：扩展结构扫描（fence 配平、`pr_number:0`、口径矛盾），决策复用 `grape_t_merge_gate_log`。
5. 远端变化判定：租约记录 `expected_remote_sha`，push 前 `ls-remote` 对比；PR 状态查询按 head 分支匹配 merged/closed。

### 关键设计决策
| 决策 | 选项 | 选择 | 理由 |
|------|------|------|------|
| 互斥粒度 | 仓库/PR/分支 | 分支（`repo_key+head_branch`） | 冲突只发生在同一 head 分支，粒度最小、误伤最少 |
| 外部 merge-bot 并发 | 强互斥/检测提示 | 检测+提示（租约开放对接） | 平台无权改造外部 bot；满足"至少可感知、可协调" |
| 守卫失败语义 | fail-open/fail-closed | 已合并/远端变化 fail-closed；查询异常 fail-open+告警 | 防止修复丢失优先；守卫自身故障不阻断正常交付 |
| 结构校验分级 | 全阻断/分级 | `pr_number:0`、元数据矛盾硬阻断；fence 配平软告警 | 避免合法 Markdown 标题/示例误杀 |
| 钩子安装位置 | 全局/每 worktree | 每 worktree（`repogit` 创建时） | 与 Issue 隔离目录一致，随 worktree 清理，不污染宿主机 |

### Affected Modules
| 模块 | 改动类型 | 说明 |
|------|---------|------|
| `src/runtime/branch_guard.py` | 新增 | 租约 + push 前置判定 + 提示文案 |
| `src/runtime/repogit.py` | 修改 | 安装/移除 pre-push 钩子 |
| `src/runtime/agent.py` | 修改 | `_submit_linked_repo_prs` 接入守卫 |
| `src/api/merge_gate.py` | 修改 | 结构校验与日志 |
| `tests/` | 新增 | 租约/守卫/结构扫描单测 |

### Acceptance Criteria
- [ ] AC1：两个写入方争同一 `repo+branch` 时仅一方获得租约，另一方收到明确"分支被 task X 占用"提示且不产生重建提交。
- [ ] AC2：PR 已 merged/closed 后向该 head 分支 push 被阻断，提示"PR #N 已合并，修复不会进入基线，请从 `<base>` 新建分支并开新 PR"，不再静默成功。
- [ ] AC3：租约记录远端 sha 与 push 时远端不一致 → 阻断并提示重新 fetch/rebase，避免盲目二次解冲突。
- [ ] AC4：合入门禁对重建提交命中 `pr_number:0`/元数据矛盾时阻断，fence 不配平告警，决策写入合入日志。
- [ ] AC5：所有现有测试通过，新增测试覆盖租约争用、merged 阻断、远端漂移、结构校验四轴。

### Risks & Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| 外部 merge-bot 不接租约 | Med | 以远端漂移检测兜底，保证可感知；推动 bot 侧对接作为后续 |
| 钩子在无 grape CLI 环境失效 | Med | 钩子失败 fail-open+任务日志告警；关键自动推送路径另做服务端校验 |
| fence 配平误报 | Low | 仅软告警，不阻断 |
| 查询 PR 状态延迟 | Low | 以 push 时实时查询为准，查询异常放行但告警 |

### Assumptions / Open Questions
- 假设 devdzr-merge-bot 为独立机器人、不受本仓库控制；本 Spec 交付"检测+提示+平台侧互斥"，强互斥需 bot 侧对接同一租约。
- 如需平台内直接禁用该 bot 对 head 分支的写权限，应另走仓库分支保护配置，不在本次编码范围。
