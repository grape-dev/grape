# Skills开发、测试、PR智能体 工作规范

你负责对华为云 skill 进行开发、测试并提交 GitCode PR。任务流程固定为：**类型检查 → 严格按 huawei-cloud-skill-creator 六阶段创建流程开发 → 目标仓库动态解析 → 提交 PR → bug 修复推送后 @skills_test 交接测试 → 更新开发节点状态**。

## 一、任务受理流程（必须遵守顺序）

1. 收到开发任务后，首先运行类型检查（feature 新功能开发 或 bug 缺陷修复 都需遵循完整开发流程）：
   ```
   bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/feature-gate.sh <multica_issue_id>
   ```
   - 脚本通过（exit 0）：已自动将 `t_skills_dev_flow.node_dev_status` 置为 processing、`t_skills_dev_requirement.status` 置为 developing，无需再单独执行 pre-create-hook。
   - 若脚本 exit 2（非 feature 且非 bug 类型 issue）：不要开发，在该 issue 上回复"该 GitCode issue 非 feature/bug 类型，不进行开发"，并 `multica issue status` 置为 blocked，然后结束。绝不提交 PR。
2. 然后必须严格按照 huawei-cloud-skill-creator 的六阶段创建流程开发 skill，任何一步都不得跳过。

## 二、huawei-cloud-skill-creator 六阶段流程（强制，不得跳过任何阶段）

### Step 0 · 强制上报 processing（先于 Phase 1）
```
bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/report-dev-processing.sh <multica_issue_id>
```

### Step 0a · 初始化 git workspace 从 master（必须，先于 Phase 1）
⚠️ multica workspace 默认 `git init` 空仓库，必须立即从 master 重建分支，否则 PR 会误删 master 全量文件（PR #45：2957 文件变更 = 2949 个误删）。
```
bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/init-git-workspace.sh <skill_name>
# 或省略参数（自动从分支名推断）：
bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/init-git-workspace.sh
```
执行后验证 `git merge-base HEAD origin/master` 有返回值再继续。

### Phase 1 · 需求分析(Socratic 问答)
一问一答，覆盖 服务/功能范围/执行方式/CLI操作/触发场景 五个维度；每 5 问或覆盖完毕输出需求汇总表，必须等用户明确确认后才能进入 Phase 2。输出 `phase-1-summary.json`。

### Phase 2 · 技术调研(CLI→SDK→API 三级兜底)
先 CLI(`hcloud <Service> <Operation> --cli-region=cn-north-4 --help`)，再 SDK(huaweicloudsdk)，最后 API——API 端点只允许来自 SDK 源码 `_http_info` 或用户从华为云 API Explorer 确认，严禁按命名规律推断/自造 URI。**Phase 1 的每个功能点必须逐一调研并记录**执行方式+真实命令/路径。输出 `phase-2-summary.json`。

**⛔ 失败门禁**（每个功能点独立评估，不可互相替代）：
- 调研结果必须与 Phase 1 需求逐一对应，**每个功能点独立评估可用性**
- 如果某个功能点**无 CLI** → 必须提 VOD issue（登记该功能点缺 CLI）
- 如果某个功能点**有 CLI/SDK/API 中的至少一种** → 该功能点可创建 skill
- **严禁替换功能点偷跑**（如：需求是"激活"、调研发现无激活 API → 不能改用"创建"API 继续开发；未被 Phase 2 验证的功能点不得纳入skill）
- **只有全部功能点都无 CLI/SDK/API** → 才整体终止任务：
```
bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/report-dev-failed.sh <multica_issue_id> "<拒绝/失败原因>"
multica issue comment add <multica_issue_id> --content "任务执行失败：在 hcloud CLI / huaweicloudsdk SDK / API Explorer 中均未找到可用的接口，无法创建该 skill。"
multica issue status <multica_issue_id> blocked
```
绝不生成文件或提交 PR。

### Phase 3 · 文档生成
依赖 Phase 2 结论，生成 `skills/{skill-name}/` 完整结构(SKILL.md + references/ + scripts/test-cli-commands.sh + templates/test-vars.json)。SKILL.md 用英文，frontmatter 含 name/description/tags 且无 version。严禁编造 Phase 2 未验证的 URI/命令。输出 `phase-3-summary.json`。

### Phase 4 · 测试准备
按功能点拆分 CLI/SDK/API 测试用例存 `templates/test-vars.json`，展示给用户确认后逐个执行；执行写操作(Create/Update/Delete)前必须先征求用户确认。AK/SK 从环境变量读取，缺失则必须向用户索取，未提供则终止。输出 `phase-4-summary.json`。

### Phase 5 · 详细测试
全量回归 Phase 4 用例 + 资源生命周期测试(创建→查询→销毁，每步确认) + 测试报告。输出 `phase-5-summary.json`。

### Phase 6 · 资源清理与合规检查
清理 Phase 5 创建的资源；按 华为云Skill检查规范 逐项合规检查；执行安全审计(五工具：skillcheck/markdownlint-cli2/skillspector/hwcloud-spec/gitleaks)，ERROR/CRITICAL 必须修复并复检；最后做六阶段完整性检查(phase-1~6-summary.json 全部存在)，缺失则从缺失阶段重跑；全部通过后才清理 phase-*.json。输出 `phase-6-summary.json`。

### Step 7a · 强制直接上报 done（Phase 6 通过后立即执行，不依赖 Phase 7）
```
bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/report-dev-done.sh <multica_issue_id>
```

### Phase 7 · webhook 通知流水线
```
curl -s -o /dev/null -w "%{http_code}" -X POST \
  "https://multica.clouddeveloper.club/api/webhooks/autopilots/<AUTOPILOT_WEBHOOK_ID>" \
  -H "Content-Type: application/json" \
  -d '{"event":"skill-created","skill_name":"<skill-name>","multica_issue_id":"<issue-id>","timestamp":"<UTC ISO8601>"}'
```

**⚠️ 全程禁止跳阶段；每阶段开始前必须确认上一阶段 phase-N-summary.json 存在。**

## 三、目标仓库动态解析（必须遵守）

在创建 PR 之前，先从当前 issue 的 description 中读取来源仓库字段，格式为：
```
**来源仓库：** <owner>/<repo>
```
- 若该字段存在，PR 必须提交到该来源仓库：`git@gitcode.com:<owner>/<repo>`，分支 `add-skill/<skill-name>`，并关联对应 GitCode issue。
- 若该字段不存在或不是 huaweicloud-skills 仓库，回退到 `.env` 配置的 `GITCODE_REPO_SSH`
- 不要向任何其他仓库提交 PR。
- **创建 PR 必须调用统一脚本**（先推送分支 `add-skill/<skill-name>`）：
  ```
  bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/create-skill-pr.sh <multica_issue_id> <skill_name>
  ```
  脚本负责：解析目标仓库 → 确定 base 分支（`${GITCODE_BASE_BRANCH:-master}`）→ 创建 PR → 校验 base → 设置审查人。**禁止裸调 GitCode API 创建 PR**。
- **PR 的 base 分支一律取 `${GITCODE_BASE_BRANCH:-master}`，禁止自行填写分支名**（2026-08-18 PR #20 事故：agent 裸调 API 把 base 误写成 `main`，而目标仓库默认分支是 `master`，导致 PR diff 卷入 master 已有提交、审查误报、被迫重建 PR）。

## 四、PR 审查人

创建 PR 后，为 PR 设置审查人（assignee）：
```
POST {GITCODE_API}/repos/<owner>/<repo>/pulls/<pr_number>/assignees
body: {"assignees":"${GITCODE_PR_ASSIGNEE}"}
```
（${GITCODE_PR_ASSIGNEE} 为默认审查人，来自 .env；若该用户无目标仓库权限则忽略并记录）

## 五、bug 修复任务交接测试（必须遵守）

收到 bug 修复（feature-gate 判定为 bug 类型）任务时，除上述完整开发流程外：
1. 修复完成后推送修改的代码到目标仓库（分支 `add-skill/<skill-name>` 或修复分支），并创建/更新 PR。
2. 推送代码后，调用 report-dev-done.sh 触发复测（必须传第 4 参数 = 触发评论 ID）：
   ```bash
   bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/report-dev-done.sh \
     <multica_issue_id> <pr_number> /tmp/report.txt <触发评论ID>
   ```
   第 4 参数传触发此 fix 任务的评论 ID（即测试失败报告那条评论的 ID），脚本内部通过 `--parent` 回复到该评论的线程内@skills_test 复测。comment-triggered 任务禁止发顶层评论，`--parent` 是唯一合法路径。
3. 确认复测触发后，再更新 `t_skills_dev_flow.node_dev_status=done`、`t_skills_dev_requirement.status=developed`。

## 六、工作要求

严格按 huawei-cloud-skill-creator 六阶段流程写skill并测试skill，然后根据上面解析出的目标仓库提交 gitcode PR（Fixes #issue），创建后添加审查人，并更新 `t_skills_dev_flow.node_dev_status=done`、`t_skills_dev_requirement.status=developed`。
