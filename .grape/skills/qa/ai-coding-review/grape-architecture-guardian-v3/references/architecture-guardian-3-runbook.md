# grape-architecture-guardian-v3 执行走查表（runbook）

> 目的：把架构守护 3.0 的每一步 + 确切命令固化，Agent **照着执行，不查 `--help` / `grape commands` 逐条探索**。
> 环境变量直接使用：`$REPO_OWNER` / `$REPO_NAME` / `$REPO_FULL_NAME` / `$ISSUE_NUMBER` / `$PR_NUMBER` / `$GRAPE_ISSUE_ID` / `$GRAPE_SCENE_ID` / `$BOT_USERNAME`。
> **评论前缀硬规**：报告 body 一律以 `[QA Agent]` 开头。
> **纯审查**：审查与取证全只读（拉材料 + 只读脚本 + 发布报告）；禁改文件、禁 git 写操作、禁合并 PR。
> **规则权威**：`references/grape-architecture-rules-v3.md`（24 条 + 四级 + Gate 映射）。逐条对照，不凭记忆转述。

### Step 0 · 前置 + 读任务上下文（必须最先）

```bash
grape tasks current --json-out
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
grape events detail --seq {seq} --json-out          # 读 A2A payload：模式、pr_number、审查范围
grape git-repo pulls comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out 2>/dev/null || true
```

- 从 payload/context 提取：**模式**（audit/design/plan/review/security/scale/verify）、审查对象（PR 编号或 commit sha/文件清单）、审查范围。
- `$PR_NUMBER` 未注入 → 从 A2A payload 的 `pr_number` 取；仍缺 → `grape git-repo pulls list --owner "$REPO_OWNER" --repo "$REPO_NAME" --state open --json-out` 找待审 PR；无法确定 → 评论说明 + 收尾（next_step=reply_comment）。
- 模式推断不出且上下文无法回答 → 按 `review`（最保守，产出唯一裁决）执行，并在报告首行标注「模式推断：review」。

### Step 1 · 模式执行矩阵（必做，先定模式再取材料）

| 模式 | 何时用 | 材料 | 必经步骤 | 产出 |
|------|--------|------|----------|------|
| `review` | 审一个 PR 或一组具体变更 | PR diff/文件/commits（或 commit sha 范围） | Step 2→3→4→5→6→7，**输出唯一裁决** | 审查报告（唯一裁决 + 发现清单） |
| `audit` | 审一个已有系统整体架构 | 仓库树/关键配置文件/代码 | Step 2(仓库级)→3→4→5→6→7 | `templates/audit-report.md` |
| `design` | 实现前设计新应用架构 | 需求/技术栈意图（仓库现状作参照） | 无变更 diff → Step 3(栈盘点)+4(适用规则)+ADR+7 | `templates/architecture-decision.md` + 数值登记 |
| `plan` | 重建/迁移计划 | 先 audit 或至少 discovery | Step 2-4 → `templates/migration-plan.md` → 6→7 | 迁移计划（步骤/依赖/回滚） |
| `security` | 深度安全审计 | 代码+配置（鉴权/密钥/输入面/日志） | Step 2→3→4（A9/A11/C1-C3 全查）+`references/security.md` | 报告 + `templates/finding.md` |
| `scale` | 深度扩展性/性能审计 | 代码+配置+部署拓扑 | Step 2→3→4（A3/A4/B2 数值五源）+`references/scalability.md` | 报告 + `templates/numerical-recommendation.md` |
| `verify` | 验证整改是否真的生效 | 整改 commit/PR + 原发现清单 | 逐条对照原发现 → 匹配验证方式（§Step 5 表）→ 6→7 | 逐条结论：已关闭 / 未关闭 / 证据不足 |

- 模式决定取材范围与模板，**规则包 24 条在各模式下都逐条判定**（不适用的写「不适用」及理由）。
- 领域细则文件按需加载：`references/standards.md`、`architecture.md`、`security.md`、`scalability.md`、`databases.md`、`caching.md`、`async-queues.md`、`reliability.md`、`observability.md`、`devsecops.md`，**只加载当前步骤需要的**。

### Step 2 · 拉取审查材料（必做，全只读）

```bash
# 仓库与结构
grape repos project info --owner "$REPO_OWNER" --repo "$REPO_NAME" --json-out
grape git-repo tree --owner "$REPO_OWNER" --repo "$REPO_NAME" --depth single --json-out
grape git-repo repo --owner "$REPO_OWNER" --repo "$REPO_NAME" --json-out

# PR / 变更材料
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
grape git-repo commits --owner "$REPO_OWNER" --repo "$REPO_NAME" --json-out
grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path "{path}" --ref "{head_sha}" --raw
```

- 记录：PR 编号、`head sha` 前 8 位、base 分支、变更量（文件数/±行数）、变更文件清单（按目录聚合，用于 B3/B4/A8 判定）。
- **PR 无关联 Issue 场景**（`$ISSUE_NUMBER` 未注入/非数字）：跳过 Issue 相关命令，结论走 PR 评论；**不发起 A2A**（issue_number 必须为数字）。
- 变更文件覆盖度：`pulls files` 默认 `per_page=100`，超大 PR 用 `--page`/`--per_page` 拉全；仍不全则 `git-repo tree --depth single` 按目录**逐层**拉取，或只读 `grape git ls-tree -r` 兜底。⚠️ GitCode 不支持 `--depth recursive`（返回 HTTP 400「暂不支持递归树」），**不要**用它兜底。

### Step 3 · 跑 3 只读脚本取证（必做）

```bash
# 技能目录绝对路径（与当前工作目录无关，可对任意被审仓库执行）
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
[ -d "$SKILL_DIR/scripts" ] || SKILL_DIR=".grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3"
REPO_PATH="${REPO_PATH:-.}"          # 被审仓库根目录（含 src/ 或 tests/ 的层级）

python3 "$SKILL_DIR/scripts/repo_inventory.py" "$REPO_PATH" --json                 # 技术栈盘点 → 决定 Gate 适用性
python3 "$SKILL_DIR/scripts/magic_number_audit.py" "$REPO_PATH" --min-severity medium   # B2 无来源数值候选
python3 "$SKILL_DIR/scripts/dependency_inventory.py" "$REPO_PATH" --json           # 依赖/锁文件/固定版本
```

- 三只脚本零依赖（标准库）、**只读**、可独立运行；脚本内部路径按 `Path(__file__).resolve().parent` 解析，不写死绝对路径。
- 脚本输出是**候选与线索，不是结论**：每条都要按 §8 证据契约复核（证据是否成立、是否需人工复核）。
- 发现洪水 → `--min-severity high` 收敛 + 只对变更文件逐条人工核验。
- 脚本不存在（技能未随被审仓库检出）→ 从 grape 主仓按 `git-repo contents --path .grape/skills/qa/ai-coding-review/grape-architecture-guardian-v3/scripts/{f}.py --ref {sha} --raw` 取回后本地执行，不编造脚本输出。

### Step 4 · 逐条规则四态判定（必做）

按 `references/grape-architecture-rules-v3.md` 逐条走完 24 条（A1-A14 / B1-B5 / C1-C3 / D1-D2）：

- 每条落到四态之一：**符合**（给验证方式）/ **不符合**（**必须**带 `file:line` 或 `commit sha`）/ **不适用**（写理由）/ **证据不足**（`INSUFFICIENT DATA TO VERIFY` + 缺哪种测量）。
- **B5 原文无内容 → 一律标「不适用（规则内容待补充）」**，禁止编造约束、禁止据此下裁决。
- 每条「不符合」按 `templates/finding.md` 记录：规则编号 + Severity + file:line + 问题 + 故障场景 + 修复 + 验证方式 + **修复顺序与依赖**。
- 无 `file:line`/`commit sha` 证据 → 标「证据不足，需人工复核」，**不判 BLOCKED**。
- Gate 维度（§3 7 Gate）作为交叉检查视角：某 Gate 下的规则全部「不适用」时，报告该 Gate 标「不适用」并给理由。
- 反幻觉自检（每条技术声明前）：能被仓库代码、项目文档、标准或实测数据支持吗？不能则不作事实陈述；对数值这是绝对的。

### Step 5 · 汇总报告与唯一裁决（必做）

**review 模式（对 PR 或具体变更）：唯一裁决，三选一，不得并列或模糊**

| 裁决 | 触发条件 | 后附内容 |
|------|----------|----------|
| `APPROVED` | 0 BLOCKED、0 Critical；Medium 已在首屏列出待人工判断 | 无 |
| `APPROVED WITH CONDITIONS` | 存在 Critical（可整改）或仅 Medium 需整改 | **列出整改条件**（对应规则编号 + file:line） |
| `BLOCKED` | 存在 BLOCKED 级不符合（且有 file:line/commit sha 证据） | **给出具体技术缺陷**（规则 + file:line + 故障场景），不是风格偏好 |

- 报告**首屏**必须依次给出：唯一裁决 → 级别统计 `{N} BLOCKED / {N} Critical / {N} Medium / {N} Informational` → **「需人工判断是否合入」的 Medium 清单**（规则编号 + 一句话影响）→ 合入映射。
- **Medium 项只进首屏清单，不置平台 waiting 状态**（本技能 `flow_nodes=无`，不调用 `issues pending-review`）。
- `BLOCKED` 须可复核：无证据不判死；裁决与被判规则、证据一一对应。
- 每条发现按 §8 证据契约输出；**禁止编造数值风险分**。
- 其他模式：`audit` → `templates/audit-report.md`；`design` → `templates/architecture-decision.md`；`plan` → `templates/migration-plan.md`；`verify` 逐条给「已关闭/未关闭/证据不足」并附验证方式。
- 涉及数值建议 → 必附 `templates/numerical-recommendation.md` 块；缺源则 `<undecided: 需要何种测量>` + `INSUFFICIENT DATA TO VERIFY` + 待决策清单（报告末尾独立小节「待决策清单」）。
- 敏感值一律掩码（`ghp_****`、`192.168.****`），只留 file:line/commit sha 定位。

**验证方式与发现类型匹配（`verify` 模式判定关闭的依据）**

| 发现类型 | 验证方式 |
|---|---|
| 安全 | 安全测试证明攻击路径已关闭 |
| 查询/数据 | 执行计划或基准测试（前后对比） |
| 扩展性 | 压测 |
| 并发/竞态 | 并发测试 |
| 队列 | 失败与重试测试 |
| 迁移 | 迁移 + 回滚测试 |
| 恢复 | 实际恢复演练 |

### Step 6 · 发布报告（必做，唯一写操作）

```bash
# PR 审查：发 PR 评论
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body":"[QA Agent] 架构守护 3.0 审查报告（PR #'"$PR_NUMBER"'｜head sha: {sha8}｜模式 {mode}）\n\n{首屏裁决 + 统计 + Medium 清单}\n\n{报告正文}"}' --json-out

# Issue 关联场景（无 PR 编号时）：发 Issue 评论
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body":"[QA Agent] 架构守护 3.0 审查报告（Issue #'"$ISSUE_NUMBER"'｜模式 {mode}）\n\n{报告正文}"}' --json-out
```

- 返回的 `comment id` 记入收尾 artifacts。
- 已有同轮同 head sha 的报告（`[QA Agent]` 前缀）→ 不重复发布，直接收尾。
- 发布失败重试 ≤3 次；仍失败 → 另一入口评论说明 + 标注人工介入（`pulls comments create` 返回 422 时可降级 `issues comments create --number {PR_NUMBER}`）。

### Step 7 · 收尾 / 交办（必做，任何分支执行到最后）

1. 输出人类可读摘要：审查对象、模式、唯一裁决、四级计数、报告 comment id、待决策清单条数。
2. **A2A 前先做场景节点核验**（未核验不发起）：

```bash
SCENE_NODES="$(grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out 2>/dev/null)"
echo "$SCENE_NODES" | jq -r '[.nodes[]?.tag // .nodes[]?.skill] | unique | join(",")' 2>/dev/null
```

- `BLOCKED` / 需整改 → 命中 `coding` 节点时交办：`grape a2a handoff --json '{"agent":"coding","payload":{"repository":"'"$REPO_FULL_NAME"'","account":"'"$ACCOUNT"'","issue_number":'"$ISSUE_NUMBER"',"pr_number":'"$PR_NUMBER"',"context":"架构守护 3.0 审查结论 {裁决}，{N} 项 BLOCKED / {N} 项 Critical 需按报告整改（含 file:line 与修复顺序）"}}' --json-out`，`next_step=a2a_coding`。
- `APPROVED` 且流程需要放行 → 命中 `sre` 节点时交办 `{"agent":"sre",...}`，`next_step=a2a_sre`。
- 未命中目标节点 / `$ISSUE_NUMBER` 非数字 / Medium 待人工判断 → **不 A2A**，报告已发布即收尾，`next_step=reply_comment`。
- 最后 `grape tasks complete`（模板见 SKILL.md），`already:true` 忽略。

## 错误排查速查

| 现象 | 处理 |
|------|------|
| `No such command 'X'` | 命令名错，核对 `references/grape-cli-reference.md`；仍不确定 `grape commands <关键词>` |
| `pulls get` / API 5xx | `grape tasks wait --task-id` 或 CLI 内置退避重试；仍失败 `grape git ls-remote origin` 核对远端 ref 后再继续 |
| 脚本 `not a directory` / 输出为空 | `REPO_PATH` 必须是仓库根（含 `src/` 或 `tests/` 的层级），不是子目录 |
| 脚本结论与代码矛盾 | 以代码/配置 file:line 为准，脚本输出降级为线索并在报告注明 |
| `pulls files` 清单不全 | `--page`/`--per_page` 翻页拉全；目录层级用 `git-repo tree --depth single` 逐层拉取或 `grape git ls-tree -r`（`--depth recursive` 仅 GitHub 支持，GitCode 返回 400） |
| 评论发布 422 | 降级 `git-repo issues comments create --number {PR_NUMBER}`；仍失败 → 说明并标注人工介入 |
| 401 凭据失效 | 说明「凭据已失效」，不换 token |
| 403 账户/平台类端点 | 需 `grape_`/JWT；本技能一般用不到，不绕行 |

## 明确禁止

- ❌ 修改文件 / git 写操作 / 合并 PR（纯审查）。
- ❌ 无 `file:line`/`commit sha` 证据判 BLOCKED。
- ❌ 编造数值风险分、编造标准条款 ID。
- ❌ 数值无来源仍写最终值（必须 `<undecided: 需要何种测量>` + `INSUFFICIENT DATA TO VERIFY`）。
- ❌ 为 B5 编造约束条款。
- ❌ 调用 `issues pending-review`（本技能 `flow_nodes=无`，Medium 走报告首屏人工清单）。
- ❌ A2A 交办写技能名（交办 **agent**：`coding` / `sre`）。
- ❌ 因「等待」跳过最终收尾（`tasks complete`）。