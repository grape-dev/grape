# production-architecture-guardian 执行走查表（runbook）

> 目的：把两种模式（audit / review）的每一步 + 确切命令固化，Agent **照抄执行，不逐条 `--help` / `grape commands` 探索**。
> 环境变量直接使用：`$REPO_OWNER` / `$REPO_NAME` / `$REPO_FULL_NAME` / `$ISSUE_NUMBER` / `$PR_NUMBER` / `$GRAPE_ISSUE_ID` / `$GRAPE_SCENE_ID`。
> **评论前缀硬规**：报告 body 一律以 `[QA Agent]` 开头。**纯审查**：禁改文件、禁 git 写操作、禁合并 PR。
> 规则与检查项：`references/architecture-rules-baseline.md`（23 条）+ `references/audit-checklists.md`（43 条检查项，四段式）。逐条对照，不凭记忆转述。

### Step 0 · 前置 + 读任务上下文（必须最先）

```bash
grape tasks current --json-out
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
grape events detail --seq {seq} --json-out        # A2A payload：模式、pr_number、审查范围
grape git-repo pulls comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out 2>/dev/null || true
```

- 提取：**模式**（audit / review）、审查对象（PR 编号 / commit sha / 文件清单）、审查范围与既有报告（防重复发布）。
- `$PR_NUMBER` 未注入 → 取 A2A payload `pr_number`；仍缺 → `grape git-repo pulls list --owner "$REPO_OWNER" --repo "$REPO_NAME" --state open --json-out`；无法确定 → 评论说明 + 收尾（`next_step=reply_comment`）。
- 模式推断不出 → 按 `review`（最保守，产出唯一裁决）执行，报告首行标注「模式推断：review」。

### Step 1 · 模式判定与取材范围

| 模式 | 何时用 | 材料 | 必经步骤 | 产出模板 |
|------|--------|------|----------|----------|
| `audit` | 既有系统整体架构审计 | 仓库树 / 关键配置 / 代码 / 依赖清单 | Step 2→3→4a→5→6→7→8 | `templates/audit-report.md` |
| `review` | PR 或一组具体变更的合入门禁 | PR diff / files / commits（或 sha 范围） | Step 2→3→4b→5→6→7→8，**输出唯一裁决** | `templates/gate-decision.md` |

- 两模式共用：脚本取证、数值证据门、报告发布、收尾；差异只在 Step 4 的判定对象集合（43 条检查项 vs 17 检查面）。

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

- 记录：PR 编号、`head sha` 前 8 位、base 分支、变更量（文件数/±行数）、按目录聚合的变更文件清单（供 A-2/A-3/B-3 判定）。
- 变更文件覆盖度：`pulls files` 默认单页有上限，超大 PR 用 `--page`/`--per_page` 翻页；仍不全则 `tree --depth single` 逐层拉或 `grape git ls-tree -r` 兜底。⚠️ GitCode 不支持递归树（HTTP 400），**不要**用 `--depth recursive`。
- 无关联 Issue 场景（`$ISSUE_NUMBER` 非数字）：跳过 Issue 命令，结论走 PR 评论，**不发起 A2A**。

### Step 3 · 三支只读脚本取证（必做）

```bash
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
[ -d "$SKILL_DIR/scripts" ] || SKILL_DIR="${DEVOPS_SKILLS_DIR:-.grape/skills}/qa/production-architecture-guardian"
REPO_PATH="${REPO_PATH:-.}"          # 被审仓库根目录

python3 "$SKILL_DIR/scripts/repo_inventory.py"      "$REPO_PATH" --json                     # 技术栈 → Gate/检查面适用性
python3 "$SKILL_DIR/scripts/magic_number_audit.py"  "$REPO_PATH" --json --min-severity medium  # B2 未举证数字（机器可校验）
python3 "$SKILL_DIR/scripts/dependency_inventory.py" "$REPO_PATH" --json                    # 依赖/锁文件/固定版本
```

- 三支脚本零依赖（标准库）、只读、可独立运行；内部路径按 `Path(__file__).resolve().parent` 解析，**不写死绝对路径**。
- 脚本输出是**候选与线索，不是结论**：每条按检查项「所需证据」复核；发现洪水 → `--min-severity high` 收敛，只对变更文件逐条人工核验。
- 脚本不可用（技能未随被审仓库检出）→ 用 `git-repo contents --path {skill 内脚本路径} --ref {sha} --raw` 取回后本地执行，**不编造脚本输出**。

### Step 4a · audit 模式：phases A–J 检查项逐项判定（必做）

按 `references/audit-checklists.md` 走完 **43 条**（A-1…J-3），每条落四态并给级别：

- 符合（给验证方式）/ 不符合（**必带 `file:line` 或 `commit sha`**）/ 不适用（写理由）/ 证据不足（`INSUFFICIENT DATA TO VERIFY` + 缺哪种测量）。
- 级别字段只取四级（`BLOCKED`/`Critical`/`Medium`/`Informational`）；每条注明「依据」（规则编号 / references 章节 / templates / scripts），不得出现无依据检查项。
- 汇总按 phase 呈现：每 phase 给「项数 / 符合 / 不符合 / 不适用 / 证据不足」计数；未覆盖的 phase 明确写「本次范围未涉及」及理由。

### Step 4b · review 模式：17 检查面 + Architecture Gate（必做）

按 `references/gate-review.md` §2 逐面给**最低证据**，达不到即记「证据不足」（影响裁决）：

- 逐面结论 + 证据定位（`file:line` / `commit sha` / 脚本输出），17 面不得空面（不适用要写理由）。
- 套 `references/gate-review.md` §3 阻断条件清单判 `BLOCKED`；否则按 §1 三态定义出唯一裁决。
- `APPROVED WITH CONDITIONS` 的条件按 `templates/gate-decision.md` §2 四段式（条件内容/举证要求/解除条件/跟踪方式）逐条给出，缺段即条件无效。

### Step 5 · 数值证据门核对（必做，两模式共用）

1. 取 Step 3 `magic_number_audit.py --json` 的 `unbacked_numbers[]`。
2. 逐条结案：举证（五源之一 + 定位）｜例外登记（`primary_source`/`measured`/`business_requirement`/`industry_standard`）｜转 `<undecided>` + 待决策清单。规则见 `references/numerical-evidence-gate.md` §6。
3. 报告写入「未举证数字清单」表（`templates/audit-report.md` §3），条数与 JSON `summary.unbacked` 对齐。
4. 数值证据面证据不足 → review 模式裁决 `BLOCKED`；audit 模式 H-4 判「不符合」（`Critical`）。

### Step 6 · 汇总与裁决（必做）

**首屏顺序（硬性）**：唯一裁决/总体结论 → 四级统计 `{N} BLOCKED / {N} Critical / {N} Medium / {N} Informational` → 需人工判断的 Medium 清单（编号 + 一句话影响）→ 待决策清单条数。

- 每条发现按 `templates/finding.md` 输出（编号 + 级别 + 证据 + 故障场景 + 修复顺序与依赖），**禁止编造数值风险分**。
- 敏感值掩码（`ghp_****`、`192.168.****`），只留 `file:line` / `commit sha` 定位。
- 结论标注证据分级（`verified: code/config/vendor/standard`、`measured`、`load-tested`、`plausible`、`INSUFFICIENT DATA TO VERIFY`）。

**验证方式与发现类型匹配**

| 发现类型 | 验证方式 |
|---|---|
| 安全 | 安全测试证明攻击路径已关闭 |
| 查询/数据 | 执行计划或基准测试（前后对比） |
| 扩展性 | 压测 |
| 并发/竞态 | 并发测试 |
| 队列 | 失败与重试测试 |
| 迁移 | 迁移 + 回滚测试 |
| 恢复 | 实际恢复演练 |

### Step 7 · 发布报告（必做，唯一写操作）

```bash
# PR 审查：发 PR 评论
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body":"[QA Agent] 架构守护审查报告（PR #'"$PR_NUMBER"'｜head sha: {sha8}｜模式 review）\n\n{首屏裁决 + 四级统计 + Medium 清单 + 待决策条数}\n\n{报告正文}"}' --json-out

# Issue 场景（无 PR 编号）：发 Issue 评论
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body":"[QA Agent] 架构守护审计报告（Issue #'"$ISSUE_NUMBER"'｜模式 audit）\n\n{报告正文}"}' --json-out
```

- 返回的 `comment id` 记入收尾 artifacts；已有同轮同 `head sha` 的报告（`[QA Agent]` 前缀）→ 不重复发布。
- 发布失败重试 ≤3 次；仍失败 → 另一入口评论说明并标注人工介入（`pulls comments create` 422 时可降级 `issues comments create --number {PR_NUMBER}`）。

### Step 8 · 收尾 / 交办（必做，任何分支执行到最后）

1. 输出人类可读摘要：审查对象、模式、裁决、四级计数、报告 comment id、未举证数字条数。
2. **A2A 前先核验场景节点**（未核验不发起）：

```bash
SCENE_NODES="$(grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out 2>/dev/null)"
echo "$SCENE_NODES" | jq -r '[.nodes[]?.tag // .nodes[]?.skill] | unique | join(",")' 2>/dev/null
```

- `BLOCKED` / 需整改且命中 `coding` 节点 →
  `grape a2a handoff --json '{"agent":"coding","payload":{"repository":"'"$REPO_FULL_NAME"'","account":"'"$ACCOUNT"'","issue_number":'"$ISSUE_NUMBER"',"pr_number":'"$PR_NUMBER"',"context":"架构守护审查结论 {裁决}，{N} 项 BLOCKED / {N} 项 Critical 需按报告整改（含 file:line 与修复顺序）"}}' --json-out`，`next_step=a2a_coding`。
- `APPROVED` 且需放行 → 命中 `sre` 节点时交办 `{"agent":"sre",...}`，`next_step=a2a_sre`。
- 未命中目标节点 / Issue 号非数字 / 已发评论待人工判断 → **不 A2A**，`next_step=reply_comment`。
- 最后 `grape tasks complete`（模板见 SKILL.md「任务完成流程」），`already:true` 忽略。

## 错误排查速查

| 现象 | 处理 |
|------|------|
| `No such command 'X'` | 命令名错，核对 `references/grape-cli-reference.md`；仍不确定 `grape commands <关键词>` |
| `pulls get` / API 5xx | `grape tasks wait --task-id` 或 CLI 内置退避重试；仍失败 `grape git ls-remote origin` 核对远端 ref 后再继续 |
| 脚本 `not a directory` / 输出为空 | `REPO_PATH` 必须是仓库根（含 `src/` 或 `tests/` 的层级），不是子目录 |
| 脚本结论与代码矛盾 | 以代码/配置 `file:line` 为准；脚本输出降级为线索并在报告注明 |
| `pulls files` 清单不全 | `--page`/`--per_page` 翻页；`tree --depth single` 逐层拉或 `grape git ls-tree -r` 兜底 |
| 评论发布 422 | 降级 `git-repo issues comments create --number {PR_NUMBER}`；仍失败 → 说明并标注人工介入 |
| 401 凭据失效 | 说明「凭据已失效」，不换 token |
| 403 账户/平台类 | 需 `grape_`/JWT；本技能一般用不到，不绕行 |

## 明确禁止

- ❌ 修改文件 / git 写操作 / 合并 PR（纯审查）。
- ❌ 无 `file:line`/`commit sha` 证据判「不符合」；关键面证据缺失却给 `APPROVED`。
- ❌ 数值无来源仍写最终值（必须 `<undecided: 需要何种测量>` + `INSUFFICIENT DATA TO VERIFY`）。
- ❌ 编造规则条款、编造标准条款 ID、编造数值风险分。
- ❌ 检查项编号与规则编号出现孤儿（无依据检查项 / 未被索引规则）。
- ❌ A2A 交办写技能名（交办 **agent**：`coding` / `sre`）。
- ❌ 因「等待」跳过最终收尾（`tasks complete`）。
