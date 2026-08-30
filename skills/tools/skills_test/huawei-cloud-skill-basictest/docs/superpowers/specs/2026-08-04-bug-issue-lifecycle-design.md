# BUG Issue 生命周期设计（创建 + PR 更新重测闭环）

日期：2026-08-04
状态：已批准（用户确认 ①-⑦ 设计）

## 背景与目标

当前 skill 测试流水线（`huawei-cloud-skill-basictest` + `deploy/agent_test_instructions.md`）规定：
**测试发现 BUG 时不创建 GitCode BUG issue**，BUG 仅通过上传到 PR 的总测试报告体现
（basictest SKILL.md 7.4）。本设计改变该行为，引入完整的 BUG issue 生命周期：

1. 测试发现 BUG 且 PR 创建人不是 `skills_bot` 时，创建带 `BUG` 标签的 issue，
   责任人为 PR 创建人，内容描述 BUG 详情与修复建议。
2. PR 重新提交或内容更新时，重新测试该 PR 关联的 BUG 是否仍存在：
   - 仍存在 → 在原 BUG issue 下留言"还未修复"。
   - 已修复 → 关闭该 BUG issue。

## 术语

- **BUG issue**：GitCode issue，标题格式 `测试发现BUG: {skill} (PR #{n})`，标签 `BUG`。
- **PR 创建人**：GitCode PR 的 `user.login` 字段。
- **skills_bot**：自动化流水线账号（`skill-auto-test-pr` 提交流水线使用的 PR 创建账号）。
- **重测任务**：Multica 中 `requirement_type='retest'` 的测试任务，由 PR update 事件派发。

## 架构决策（用户已确认）

- **实现方案 A**：BUG issue 的创建/留言/关闭全部由「专业功能测试」agent 在测试运行内
  通过 `gitcode_api.sh` 完成；`gitcode_event_handler.py` 仅负责"PR update 事件 → 检测
  已打开 BUG issue → 派发重测 Multica 任务"。
- **BUG issue 创建时机**：测试时由测试 agent 创建（改造 basictest skill 7.4 + agent 指令）。
- **重测触发机制**：PR update 事件驱动（gitcode-event-handler 派发重测任务给测试小队）。
- **BUG issue 与 PR 关联方式**：GitCode 查询关联（PR URL 在 issue body 的 `- 来源:` 行 +
  标题含「测试发现BUG」+ 标签 BUG），不依赖数据库新增字段。
- **命名与去重**：标题沿用 `测试发现BUG: {skill} (PR #{n})`；已存在打开的 BUG issue 则
  不新建，直接复用。
- **重测范围**：全量重跑 basictest，按原 BUG issue 记录的问题单判定是否复现。
- **skills_bot 判定**：`author/login == "skills_bot"`。
- **实现位置**：`/root/skills_test` 仓库源 + `/root/.agents/skills/` 已部署两份同步修改。

## 组件一：gitcode_api.sh 新增子命令

所有新增子命令遵守现有封装约定（`PRIVATE-TOKEN` header、退出码语义、`get_token()`、
`req()`/`req_ok()`/`req_code()` 辅助函数）。

| 子命令 | 实现要点 |
|--------|----------|
| `issue comment <owner> <repo> <n> --body <text>` | `POST /repos/{o}/{r}/issues/{n}/comments`；校验 body 非空；成功打印 comment id；非 2xx 退出 3 |
| `issue close <owner> <repo> <n>` | `PATCH /repos/{o}/{r}/issues/{n}` body `{"state":"close"}`；成功打印 `✅ issue #n 已关闭` |
| `issue find-by-pr <owner> <repo> <pr> [--label BUG]` | 分页 `GET /repos/{o}/{r}/issues?labels=BUG&per_page=100&page=N` 遍历；筛选 `state=open`；匹配 body 含 `https://gitcode.com/{owner}/{repo}/pull/{pr}` 或 `/merge_requests/{pr}`；标题含 `测试发现BUG` 且含 `(PR #{pr})` 兜底；输出空格分隔的 issue 编号列表（无则空） |

复用现有命令：
- `issue create`：已支持 `--title` / `--body` / `--body-file` / `--assignee` /
  `--assignee-from-pr` / `--pr-url`（自动并入 `- 来源:` 行）。
- `issue labels-set`：打标签（含生效验证）。
- `pr get`：取 PR 创建人 `user.login`。

## 组件二：basictest SKILL.md 7.4 改造（有 BUG 时）

当前 7.4 规定"有 BUG 时不创建 GitCode BUG issue"。改造为：

1. `gitcode_api.sh pr get <owner> <repo> <pr>` 取 `user.login` 作为 PR 创建人。
2. **PR 创建人 == `skills_bot`** → 不建 BUG issue（沿用现状，仅上传总测试报告到 PR，
   指派 Dengbiqiang）。`hd_skill_test_detail.issue_url` 留空。
3. **PR 创建人 != `skills_bot`**：
   - 先 `issue find-by-pr <owner> <repo> <pr> --label BUG` 查已打开的 BUG issue。
   - **存在** → 不新建，复用该 issue（把最新问题单追加为 issue 评论）。
   - **不存在** → 创建：
     - 标题：`测试发现BUG: {skill} (PR #{n})`
     - 标签：`BUG`（`issue labels-set`）
     - assignee：PR 创建人（`--assignee`，即 author/login）
     - body：问题单内容（含严重级别、复现、期望/实际）+ 修复建议（问题单"改进点"字段）
       + `--pr-url {PR_URL}` 并入来源行
   - `hd_skill_test_detail.issue_url` 填 BUG issue 的 html_url（有 BUG 且已创建时）。

创建失败（无凭证/网络/仓库不存在/无权限）不阻塞流程，在报告中注明原因（沿用现状）。

## 组件三：basictest 新增 7.8「重测与 BUG 闭环」

触发条件：本任务为重测任务（Multica 任务 body 含"重测"标记，或该 PR 已存在打开的
BUG issue）。

流程：
1. 全量重跑 basictest 评测 → 生成问题单。
2. 对每个已打开的 BUG issue（`issue find-by-pr --label BUG`）判定原 BUG 是否仍复现
   （按原 issue 问题单逐条对最新问题单比对）：
   - **仍复现** → `issue comment` 留言"还未修复"，附最新复现证据（失败用例/报错）。
     幂等：同一 BUG 已留言"还未修复"则更新为最新证据，不产生重复刷屏。
   - **未复现（已修复）** → `issue close` 关闭该 BUG issue，并 `issue comment`
     "已修复"（可选）。
3. 重测结果照常：总测试报告上传 PR、指派测试人 Dengbiqiang、更新 DB 测试节点。

## 组件四：agent_test_instructions.md 改造

1. 第三节"存在 BUG 时"：
   - 删除"不创建 GitCode BUG issue"表述。
   - 改为：判定 PR 创建人（`gitcode_api.sh pr get`）。
     - `== skills_bot` → 不创建 BUG issue（仅上传总测试报告 + 指派）。
     - `!= skills_bot` → 按组件二第 3 步创建/复用 BUG issue。
   - `hd_skill_test_detail.issue_url` 有 BUG 且已创建时填写。
2. 新增"重测任务"识别：任务描述/评论含"重测"标记或 PR 已有打开 BUG issue →
   走组件三重测闭环（留言"还未修复"或关闭），不新建 BUG issue。

## 组件五：gitcode_event_handler.py PR update 重测派发

在 `handle_pr(payload)` 处理 PR update 事件（非首次 open）时新增一步：

1. `pr_number`/`repo` 已由现有逻辑解析。
2. 调 `issue find-by-pr`（或脚本内直接调 GitCode API）查该 PR 已打开的 BUG issue。
3. **存在** → 派发重测：
   - 幂等守卫：`db_find_requirement_by_pr_id(pr_number, repo)` 查
     `requirement_type='retest'` 且 `status IN (dispatched, pr_created, in_progress)`
     的活跃记录 → 存在则跳过（避免 PR update 事件多次触发重复派发）。
   - 创建 Multica 任务分配给测试小队（SQUAD_TEST），标题 `[重测] PR #{n} BUG 复测`，
     body 含：PR URL、BUG issue 编号清单、"重测"标记。
   - 入库：`db_insert_requirement`（requirement_type='retest'，status='dispatched'，
     gitcode_pr_id=pr_number）+ `db_insert_flow`。
   - PR 评论通知（`[skills_bot]` 前缀）"已触发重测"。
4. **不存在** → 走原有 update 处理逻辑（现状，不改变）。

PR 创建人 == `skills_bot` 的 PR 不会创建 BUG issue（组件二），因此天然不会触发重测，
无需额外判断。

## 错误处理

- BUG issue 创建/留言/关闭失败：不阻断测试主流程，在测试报告与 issue 评论中注明原因。
- 重测派发失败：记录错误并返回，不影响原 PR update 流程（沿用 `safe_notify` 模式）。
- 幂等异常（重复事件）：沿用现有"评论去重标记 / DB 唯一索引 / ON DUPLICATE KEY"模式。

## 测试与验证

1. `gitcode_api.sh` 新子命令：mock/dry-run 校验参数校验、退出码、HTTP 路径与 body。
2. `gitcode_event_handler.py --test`：自检（数据库/GitCode/Multica/四小队）全 OK。
3. 构造 PR update payload 验证：有打开 BUG issue → 派发重测（幂等）；
   无 BUG issue → 走原逻辑。
4. 端到端：首次测试有 BUG（非 bot 创建人）→ 创建 BUG issue；PR 更新 → 重测 →
   仍复现留言 / 修复关闭。
5. 部署同步：仓库源 + `/root/.agents/skills/` 两份一致。

## 不做的事（YAGNI）

- 不新增数据库字段（不用 bug_issue_id 关联，关联靠 GitCode 查询）。
- 不做 BUG issue 到 PR 的正式"关联"（GitCode link），仅 body `- 来源:` + 标签 + 标题约定。
- 不做"定向回归原 BUG"，重测为全量。
- 不处理"PR 创建人 == skills_bot 的 PR 重测"（无 BUG issue 即无重测）。
