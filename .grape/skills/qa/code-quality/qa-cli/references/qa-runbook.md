# QA-CLI 执行走查表（runbook）

> **版本来源（version_source）**：以 `$GRAPE_WORK_BRANCH` 为开发基线；注入副本若与本基线不一致，以基线为准重读（`grape git-repo contents --ref`）。

> 目的：把 QA Agent 审查 PR 的**每一步要做什么 + 用哪条确切命令**固化下来，Agent **照着执行、不要查 `--help` / `grape commands`**。
> 环境变量可直接使用：`$ACCOUNT`（owner）、`$REPO_NAME`（repo 名）、`$ISSUE_NUMBER`、`$PR_NUMBER`、`$REPO_FULL_NAME`（owner/repo）、`$GRAPE_SCENE_ID`（当前场景）、`$GRAPE_WORK_BRANCH`（目标分支）、`$GRAPE_SESSION_TOKEN`/`$GRAPE_SERVER_URL`（grape 自动读取）。目标合入分支统一解析为 `$WORK_BRANCH`（Step A1 取 PR base，缺省仓库默认分支）。
> **评论前缀硬规**：评论 body 一律以 `[$AGENT_NAME]`（当前 Agent 名）开头；构造 `--json` body 时用双引号使 `$AGENT_NAME` 展开。

---

## 分析（每次执行先分析，必须按 0→1→2→3 顺序；上一步未完成不得进入下一步）

### Step A0 · A2A 触发识别
- 事件为 `a2a_event` → 从 prompt「A2A 目标 PR：#N」/前序摘要提取 `$PR_NUMBER`；跳过 Step A1（PR 配置检查）；变更以首次审查处理。
- `pull_request_opened` → 执行 Step A1 配置检查。
- `pull_request_updated` / `push` / `note_hook_pr` → 跳过配置检查（已在创建时查过）。

### Step A1 · PR 配置检查（仅首次 `pull_request_opened`）
```bash
# 取 PR 详情；同时解析目标合入分支（PR 的 base）供 Step 4 工作流审查核对基准
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out   # 取 head.sha / base.ref
WORK_BRANCH="${GRAPE_WORK_BRANCH:-$(grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out | jq -r '.base.ref // "main"')}"
echo "work_branch=$WORK_BRANCH"   # 缺省仓库默认分支，不要写死 main
# 需求卡片内部 id（pending-review / review-pass / review-reject 的 --issue_id 用，非 issue 编号）
# ⚠️ 优先用已注入的环境变量 $GRAPE_ISSUE_ID；未注入时才降级用 issues detail 解析（兜底）
ISSUE_ID="${GRAPE_ISSUE_ID:-$(grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out | jq -r '.id' 2>/dev/null)}"
echo "issue_id=$ISSUE_ID"   # 解析为空 → 说明无法确定 issue_id，与 issue 编号不混用
```
- ⚠️ **`$GRAPE_WORK_BRANCH` 语义（重要，避免误判）**：它是**整个仓库/场景的开发基线分支**，可能叫长期分支（`dev`/`weekly-xx`）**或 `feat/issue-N`（N 不一定等于当前 Issue/PR 编号，是场景配置的基线名）**——不是"当前 Issue 的专属分支"。
  - **默认采用 `$GRAPE_WORK_BRANCH` 作为 PR 审查的基准确认**；若该变量未注入，则以 PR 的 `base.ref`（>= 仓库默认分支）为 `$WORK_BRANCH`。
  - 即使它叫别的 Issue 分支名，也不要因名字含其它 Issue 号就丢弃/改写为 main；仅当 PR 的 `base.ref` 明确指向其它分支（stacked PR 依赖）时以 PR base 为准。
  - 优先级：`$GRAPE_WORK_BRANCH`（权威）> PR `base.ref` > 仓库默认分支 > `main` 探测。
- 按 `references/pr-config-check.md` 检查，不通过项记录到 `review_comments`（暂不评论）。
- 有关联 Issue → `grape git-repo issues comments create ... --json '{"body":"[$AGENT_NAME] 📌 关联 PR #'"$PR_NUMBER"'"}'`；对应 Issue 是 bug → 更新标签 `bug,triaged`。

### Step A2 · 变更检视
```bash
cat ../.last_pr_review/{$PR_NUMBER}.sha 2>/dev/null   # 本机标记文件
```
- 不存在 / SHA 不同 → 有新变更，继续。
- SHA 相同 → 无新变更，跳过以下所有能力项（配置检查评论仍发送），进入 Step 6 收尾。

### Step A3 · 任务类型判断
| 判断条件 | 任务类型（按顺序执行） |
|---------|----------------------|
| PR 修改 `skills/` | 常规审查 → Skill 审查 → 工作流审查 → A2A → 评论 |
| PR 含 IaC（K8s/Terraform/Dockerfile） | 常规审查 → IaC 审查 → 工作流审查 → A2A → 评论 |
| 普通代码变更 | 常规审查 → 工作流审查 → A2A → 评论 |
| 无新变更 | 不做处理，进入 Step 6 收尾 |

---

## 执行（能力项）

### Step 1 · 常规审查（5 轴：正确性/可读性/架构/安全/性能）
```bash
grape git-repo pulls comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out   # 既有行内评论
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out           # 变更文件 + diff
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out             # PR 详情（head.sha）
```
- 对每条既有评论：若问题已修复 → 标记 resolved（⚠️ 契约当前无 `pulls comments update`，先 `grape commands pulls` 确认；不存在则在汇总评论说明"已在当前 PR 修复"，不拼不存在命令）。
- 新问题 → 评论（⚠️ 契约仅支持 `body` 字段，**不支持** `commit_id`/`path`/`position` 行内定位字段，传入会报 `unknown request field(s)`）：
  ```bash
  grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
    --json '{"body":"[$AGENT_NAME] 🔴 Critical(src/config.py:42)：硬编码密钥，建议使用环境变量"}'
  ```
  - 行内定位暂不支持：问题位置以 `文件路径:行号` 前缀写进 body（如 `src/config.py:42`）。
- 记录 Critical/Required 问题摘要供 A2A。

### Step 2 · Skill 审查（仅 PR 改 `skills/`）
按 `references/skill-review-and-quality.md` 审查格式规范/完整性/引用路径/兼容性，发现问题按 Step 1 格式评论（定位以 文件路径:行号 写进 body）。

### Step 3 · IaC 审查（仅 PR 含 IaC 文件）
按表扫描：K8s（特权容器/hostPath/securityContext/runAsRoot）、Terraform（开放 bucket/ingress 0.0.0.0/0/硬编码凭据）、Dockerfile（FROM latest/ADD/root）、Helm（敏感默认/RBAC/拉取策略）。发现问题按 Step 1 格式评论。

### Step 4 · 工作流审查（Agent Work Loop 五维：任务理解/受控执行/变更验证/可靠交付/经验沉淀）
```bash
grape git-repo tree --owner "$REPO_OWNER" --repo "$REPO_NAME" --ref "$WORK_BRANCH" --depth single --json-out                          # 目录树（无基线降级时；$WORK_BRANCH 见 Step A1）
grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path AGENTS.md --raw                                # 任务理解/受控执行
grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path spec --raw                                      # 任务理解
grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path tests --raw                                    # 变更验证
grape git-repo contents --owner "$REPO_OWNER" --repo "$REPO_NAME" --path .github/workflows --json-out                   # CI 门禁
grape git-repo pulls get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out                     # 审查/合并记录
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
```
- 证据来源标注 **observed / unobserved**；未观察显式标注、不得推断/评分。
- 发现按 `references/findings-format.md` 契约记录（维度/证据/严重度/影响/修复/验收）。

### Step 5 · A2A 通知 coding（有 Critical/Required 问题需修改时）
> ⚠️ 交办前先取当前场景 nodes 并校验含 `coding` 节点，命中才交办；场景未注入且默认链路含 coding → 用默认 `coding`。
```bash
echo "scene_id=${GRAPE_SCENE_ID:+set}"
# 场景 ID 非空时：取场景 nodes → 解析可用 Agent → 校验 coding 命中
SCENE_NODES="$(grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out 2>/dev/null)"
echo "scene_agents: $(echo "$SCENE_NODES" | jq -r '[.nodes[]?.tag // .nodes[]?.skill] | unique | join(",")' 2>/dev/null)"
if echo "$SCENE_NODES" | jq -e --arg a "coding" '[.nodes[] | ((.tag // "")|ascii_downcase) + " " + ((.skill // "")|ascii_downcase)] | any(startswith($a) or startswith($a + "-"))' >/dev/null 2>&1; then
  echo "OK: scene contains coding node → 可 A2A coding"
else
  echo "BLOCK: scene (id=$GRAPE_SCENE_ID) 不含 coding 节点 → 不发起 coding A2A"
fi
```
- **命中 coding 节点 / 场景未注入** → 发起 A2A：
```bash
grape a2a handoff --json '{
  "agent": "coding",
  "payload": {
    "repository": "'"$REPO_FULL_NAME"'",
    "account": "'"$ACCOUNT"'",
    "issue_number": '"$ISSUE_NUMBER"',
    "pr_number": '"$PR_NUMBER"',
    "context": "PR #'"$PR_NUMBER"' 审查发现问题需要修改：'$REVIEW_SUMMARY'"
  }
}' --json-out
```
- ⚠️ `issue_number` 必填，`pr_number` 填 PR 编号，禁止混用；无法确定 issue_number 则不调 A2A，在评论说明。
- **场景无 `coding` 节点（BLOCK）** → **不发起 coding A2A**：汇总评论措辞温和面向用户，说明"审查已完成，但由于当前开发流水线未配置 **coding Agent**，本次未发起修改 A2A；待你确认/配置该环节后，再进行下一步动作"；`next_step` 用 `reply_comment`。**禁止用"平台返回 skipped / not_in_scene / 请人工确认节点可用性"这类疑似 bug 的硬措辞，也不要暴露 scene id / 场景内部节点信息给用户。**

### Step 6 · 评论回复（⚠️ 所有能力项执行完毕最后统一发，不得中间单独发）
```bash
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" \
  --json '{"body":"[$AGENT_NAME] 审查报告\n\n## 配置检查\n- ✅ Title：通过\n...\n## 代码审查\n✅ 通过\n\n## 工作流审查\n⚠️ 2 项发现（见下）\n\n## 安全扫描\n✅ 安全\n\n## 合并检查\n⚠️ 测试未确认"}'
```
- body 以 `[$AGENT_NAME]` 开头；按「输出规范」逐节组织；全部通过则评论「审查通过，所有检查项均正常」。
- ⚠️ **405 降级（必须执行，不得丢弃审查结论）**：`pulls comments create` 返回 405 Method Not Allowed 时，属 **GitCode v5 API 端点方法级拒绝（open/merged PR 均可触发，与 PR 是否合并无关）**，不要归因为"PR 已合并"而放弃发评论——立即降级：
  ```bash
  grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
    --json '{"body":"[$AGENT_NAME] PR #'"$PR_NUMBER"' 审查报告（PR 评论端点 405，降级发至关联 Issue）\n\n{审查报告正文}"}'
  ```
  - body 注明 PR 编号与降级原因；评论前缀 `[$AGENT_NAME]` 规则不变。
  - PR 无关联 Issue（`$ISSUE_NUMBER` 无法确定）→ 审查结论完整写入 Step 7 `tasks complete` 的 `result_text`/`conclusion`（不丢结论），并在 `actions` 标注「PR 评论 405 降级」。
- **发评论前必做「输出自检」**（模板/契约/占位符核对，见 SKILL「输出自检」），自检不通过禁止发送（重试一次，仍失败输出显式错误说明）。

### Step 6b · 审核点（pending_review）：审查通过分支**必做**（除非已合入/无需拍板）
> ⚠️ **审查通过 = 交付决策点**。凡是审查通过且 PR **未合入**、需用户确认是否合入的，**必须用 `pending-review` 显式置审核点**（飞书触达用户拍板）；**禁止在评论里只写"可合入/建议维护者确认"就直接收尾**——无审核点则用户无触达、状态不一致。
> 例外（可不置 pending-review）：PR 已合入、或纯上报无需用户拍板（如配置检查跳过）。需用户补充信息才走 `awaiting-input`（非拍板）。

```bash
# ① 置审核点（review_points=审查结论，references=PR#；changed:true=成功）
grape issues pending-review --issue_id "$ISSUE_ID" \
  --json '{"review_points":"PR #'"$PR_NUMBER"' 审查通过（{结论摘要}），请确认是否合入","references":"PR #'"$PR_NUMBER"'"}' --json-out
```
- **用户评论续接**（平台恢复 processing 后，下一轮 qa 事件按用户回复判定）：
  - 用户同意合入/完成 → `review-pass target=done` **并处理 PR 合入 + A2A 通知 sre**（仅最终合入决策；⚠️ **合入成功后必须 A2A 交办 sre，否则 SRE Agent 收不到后续部署/发布任务**）：
    ```bash
    # ① 标记审核通过（仅 pending_review 态有效）
    grape issues review-pass --issue_id "$ISSUE_ID" --json '{"target":"done","evidence":"用户确认合入（评论 {id}）"}' --json-out
    # ② 合入 PR（走 grape git-repo 合入流程；合并后在 Issue/PR 评论告知）
    grape git-repo pulls merge --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
    # ③ 合入成功 → A2A 通知 sre agent（否则 SRE 收不到后续部署/发布任务）
    grape a2a handoff --json '{
      "agent": "sre",
      "payload": {
        "repository": "'"$REPO_FULL_NAME"'",
        "account": "'"$ACCOUNT"'",
        "issue_number": '"$ISSUE_NUMBER"',
        "pr_number": '"${PR_NUMBER:-0}"',
        "context": "PR #'"${PR_NUMBER:-0}"' 审查通过，用户确认合入且 PR 已合入。请执行部署/发布流程。"
      }
    }' --json-out
    ```
    - 若 `pulls merge` 失败（如 409 不可合并/冲突）→ 评论说明不可合入原因，不强行合并，**不 A2A sre**。
  - 用户同意继续下一环节（非最终合入）→ `review-pass target=processing`：
    ```bash
    grape issues review-pass --issue_id "$ISSUE_ID" --json '{"target":"processing","evidence":"用户确认（评论 {id}）"}' --json-out
    ```
  - 用户要求修改/打回 → `review-reject`（打回重做或转澄清）：
    ```bash
    grape issues review-reject --issue_id "$ISSUE_ID" --json '{"target":"processing","reason":"用户要求修改：{要点}"}' --json-out
    ```
- ⚠️ `pending-review` 不终态化（与任务周期正交）——**本轮仍走 Step 7 `tasks complete` 收尾**；`review-pass target=done` 才置 completed，**且需显式执行 `grape git-repo pulls merge` 合入 PR + A2A 通知 sre agent**（合入成功 + A2A sre 才算最终交付闭环）。
- ⚠️ 用户 `confirm-done`/`cancel` 是最高权威，Agent 判定不覆盖；`review-pass`/`review-reject` 仅在 `pending_review` 态有效（幂等，`changed:false` 则已终态/已退出，忽略）。
- 需用户补充信息（非拍板）→ 用 `awaiting-input`（见 vod-runbook 澄清流程），不混用。

### Step 6c · Agent feedback 审视（必做，紧邻收尾前；提交可选）
> 环节必做：**tasks complete 之前**审视一次是否遇 CLI/技能体验问题、有无明确可执行建议；**仅在有明确建议时**调用下方命令上报（无建议不调用）。审视结论写入 Step 7 `actions`，便于归档可见。

```bash
# ⚠️ 有明确建议时：只需传 category/title/description/feedback_kind；
#   title 以 `[Agent feedback]` 开头（便于转 Issue 后辨识来源）；仓库/任务/技能/event_seq 由服务端从本次会话的 GRAPE_SESSION_TOKEN 自动归属（Agent 无需、也不应传）；
#   每任务 ≤2 条，429 即停不重试；`grape commands feedback` 未见命令 → 不启用，不 curl 兜底；body 不含 token/密钥/内部路径
grape feedback submit --json '{
  "category": "suggestion",
  "feedback_kind": "skill_quality",
  "title": "[Agent feedback] grape 命令与文档不符：关于 --xxx",
  "description": "source_agent=qa; skill=qa-cli; skill_version=<当前技能版本|unknown>; env=<GRAPE_SERVER_URL|unknown>; cli=<grape --version|unknown>; os=<os|unknown>; repo=<owner/repo|unknown>; evidence=<file:line>; repro=<steps>; expected=<...>; actual=<...>"
}' --json-out
# 溯源模板（单源）：env=$GRAPE_SERVER_URL；skill_version=当前技能 SKILL.md 的 version；cli=grape --version；os=uname -srm
# 契约 body 无这些字段 → 写入 description 文本；缺值写 unknown；不写 contract/snapshot_sha/branch/commit/package
```
- **审视触发**（有则上报，无则跳过）：命令/参数报错与文档不符、命令缺失、依赖 --help 反复试错、token/步骤浪费、审查/工作流流程指引与实际 CLI 行为偏差。
- **提交失败静默**：feedback 是附带动作，失败不影响后续收尾（已在 tasks complete 前，直接继续 Step 7）。

### Step 7 · 任务完成流程（必做，任何分支都必须执行到最后）
> ⚠️ **强约束：本技能收尾 = 调 `grape tasks complete`（情形一）**。任何分支最后一步都必须调用，**评论发完 ≠ 任务完成**；只调 complete，不再输出 JSON 块。

```bash
grape tasks complete --json '{
  "result_text": "结论：PR #'"$PR_NUMBER"' 审查完成（配置检查通过 / 发现 X 项问题）。",
  "next_step": "done",
  "actions": ["代码审查 + 工作流审查", "发布问题评论 + 汇总评论", "A2A 交办 coding Agent 修复", "Agent feedback 审视：{无建议 / 已提交 #xxx}"],
  "conclusion": "PR #'"$PR_NUMBER"' 审查结论：{通过 / 需修改}；发现问题 {n} 项；A2A 交办 coding",
  "artifacts": ["PR #'"$PR_NUMBER"' 审查评论（comment id=XXXXXX）", "A2A event seq=XXXX"],
  "issue_summary": "PR #'"$PR_NUMBER"' 审查完成 | 结论 {通过/需修改} | 发现 {n} 项 | A2A {已/未}交办"
}'
```
> `next_step` 取 `a2a_issue` / `reply_comment` / `a2a_need_clarify` / `done`。`already:true` 忽略。**然后结束任务**

---

## 错误排查速查

| 现象 | 处理 |
|------|------|
| `No such command 'X'` | 命令名错，核对 runbook / reference；仍不确定才 `grape commands <关键词>` |
| `unknown request field(s): ...` | `--json` 字段不在允许集，看 `--help` 的 Body fields 删多余字段 |
| 401 凭据失效 | 说明"凭据已失效"，不换 token |
| 403 账户/平台类 | 说明需 `grape_`/JWT，本技能一般不涉及 |
| `contents` 路径不存在 / 无基线 | 用 `tree --depth single` / `contents --path ""` 定位 |
| `pulls merge` 成功 | **必须跟随 A2A 交办 sre agent**（`a2a handoff agent=sre`），否则 SRE Agent 收不到后续部署/发布任务 |
| `pulls merge` 失败（409 冲突） | 评论说明不可合入原因，不强行合并；**不 A2A sre** |
| `pulls comments create` 405 | **GitCode v5 API 端点方法级拒绝（open/merged PR 均可触发，非 PR 状态问题）** → 降级 `issues comments create` 发关联 Issue 评论（body 注明 PR 编号 + 降级原因）；无关联 Issue → 结论写入 tasks complete result_text（见 Step 6 降级块） |

## 明确禁止

- ❌ 用 `cat`/`ls`/`grep` 翻 workspace / Grape 系统文件采集证据（仅 `session_history.json` 例外可 read）。
- ❌ 中间步骤单独发评论——所有评论统一最后发送。
- ❌ 直接合并 PR / 修改 PR 代码 / 删除 PR。
- ❌ **`pulls merge` 成功后不 A2A 通知 sre**（`git-repo pulls merge` 完成后必须 `a2a handoff agent=sre`，否则 SRE 收不到后续部署/发布任务）。
- ❌ 不因"等待"跳过 Step 7（tasks complete）。