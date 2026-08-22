---
name: rest-api-skill-tester
version: 0.2.0
description: |
  REST API skill 功能验证（创建后 QA 门控）：凭据/地址检测（项目知识优先）→
  跑 skill 验证功能正确性 + 规范脚本化检查（frontmatter/文件结构/生成质量6项）→
  输出 Issue 报告 + PR 评论（✅ 通过自动合并 / 🔴 失败+原因）→ A2A 交棒 SRE Agent 发布。
  子命令动态识别：从 templates/test-vars.json 读取子命令列表，不写死。
tools:
  - curl
  - git
---

# REST API Skill Tester（通用 REST API skill 功能验证）

## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 $BOT_GITCODE_TOKEN，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止该操作并回复用户，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。
5. 所有 curl / git 调用设 30s 超时（curl --max-time 30）——超时判定失败，不 hang。
6. **调用 GitCode API 一律以 `references/gitcode-api-reference.md` 为准**：严禁根据经验/记忆/猜测自行拼写 API 路径。

## 核心原则

### 专注测试验证

负责功能验证、规范检查、安全扫描、测试报告。不负责 skill 创建、修改 skill 内容（只读验证）。

### 依赖前序分析

通过 A2A 被调用时，prompt 包含 rest-api-skill-creator 的处理摘要（skill 名称/PR 编号）。必须通过 GitCode API 获取 PR 和 Issue 完整信息。

### 动态识别子命令

从 `templates/test-vars.json` 读取 creator 生成的子命令列表，不预设子命令。测试范围由 test-vars.json 决定。

### 超出能力范围时主动请求支援

测试发现需要修复 → A2A 回 Coding Agent，不自己修。

## 评论规范

- 所有 Issue/PR 评论 body 必须以 `[$AGENT_NAME]` 开头
- 测试结果评论的固定标记放在 body 开头独立行：
  - 通过：`[$AGENT_NAME] ✅ 测试通过`
  - 失败：`[$AGENT_NAME] 🔴 测试失败`

## Configuration

本 skill 无外部 API 地址配置需求。GitCode API token 从环境变量 $BOT_GITCODE_TOKEN 获取，被测 skill 的凭据从其自身的 test-vars.json 和环境变量读取。

## 场景 / When to Use

- 收到 A2A 交棒后对 REST API skill 做功能验证（QA Agent 执行）
- 收到 pull_request_merged 事件时做前置判断（grep "✅ 测试通过"标记 → 直接 A2A SRE Agent）

Don't use for: 创建 skill（creator 职责）/ 修改 skill 内容（只读验证）/ 发布 skill（publish 职责）

## 知识 / Knowledge

### 前置条件

1. **Python 3.8+** with `requests` package（REST API skill 依赖 requests）
2. **gitleaks** installed（密钥扫描——未装则降级人工检查）
3. skill 在当前 worktree（feat/issue-{N} 分支，已含 creator 的提交产物）
4. $PR_NUMBER 已注入

### 硬规则

| 行为 | 态度 |
|------|------|
| 测试通过后必须在 Issue + PR 发评论告知 | ✅ **必须做，不可跳过** |
| 测试失败后必须 A2A 回 Coding Agent | ✅ **必须做，不可跳过** |
| 测试通过后必须调合并脚本 + A2A SRE Agent | ✅ **必须做，不可跳过** |
| A2A 必须携带 issue_number 和 pr_number | ✅ **必须做** |
| 修改 skill 内容 | ❌ **禁止**（只读验证） |
| 直接合并 PR（不经合并脚本） | ❌ **禁止** |
| 读取 Grape 内部数据 | ❌ **禁止** |

## 步骤 / Steps

### 前置判断：收到 pull_request_merged 事件时

读 Issue 评论历史，查找自己之前的"[$AGENT_NAME] ✅ 测试通过"标记：
- 找到 → PR 之前已测试通过，直接 A2A SRE Agent
- 找不到 → 正常测试流程

### 1. 功能验证（Real-Environment First）

- 定位 skill：从当前 worktree 的 skills/{name}/ 读
- 执行命令前先 cd 到 skills/{name}/ 目录（test-vars.json 的 command 用相对路径如 `python3 scripts/{name}.py`，需在 skill 目录下执行）
- 读取子命令列表：从 `templates/test-vars.json` 读取 creator 生成的子命令列表（`test_cases` 数组，每项含 id/name/command/expected/method/category/description/cleanup）+ `entry_file` 字段（固定为 `scripts/{skill_name}.py`）
- API 地址获取（从项目知识/环境变量获取，不从 test-vars.json 读）：
  - 从 `test-vars.json` 的 `skill_name` 字段推导前缀（skill name 转大写下划线，如 blog-manager → BLOG_MANAGER）
  - 优先递归扫描 `.project-info/` 下所有 JSON 文件，读 `config.{PREFIX}_BASE_URL`
  - 项目知识缺失时从环境变量 `{PREFIX}_BASE_URL` 获取
  - 以上都为空 → ⛔ 终止测试
  - ⚠️ 获取到 base_url 后必须 export 成环境变量（如 `export {PREFIX}_BASE_URL=http://...`），否则脚本读不到地址会卡在交互输入
- 凭据检测（从项目知识/环境变量获取，不从 test-vars.json 读）：
  - 从 `test-vars.json` 的 `skill_name` 字段推导前缀（skill name 转大写下划线）
  - 或从被测 skill 的 SKILL.md frontmatter name 推导
  - 优先递归扫描 .project-info/ 下所有 JSON 文件，从 secrets.{PREFIX}_* 读取
  - 项目知识缺失时动态扫描 {PREFIX} 开头 + 含 USERNAME/PASSWORD/TOKEN/API_KEY 的环境变量
  - 读到后 export 到环境变量
  - auth_type 为 none 时跳过凭据检测（公开 API 不需要认证），直接用 base_url 调 API
  - ⚠️ 变量/JSON 值存在但值为空也算缺失——必须检查值非空
  - 凭据缺失 → ⛔ 终止测试
- 测试策略（按子命令的 HTTP 方法分类，动态适配）：
  - **只读查询类**（GET 方法端点：list-* / get-*）→ 自动真实调用 API，验证返回数据
  - **变更类**（POST/PUT/DELETE 方法端点：create-* / update-* / delete-*）→ 自动执行 + 自动清理（create 后立刻 get 验证，再 delete 清理，不留垃圾数据）
  - **资源生命周期测试**：自动执行 create-{resource} → get-{resource} 验证 → delete-{resource} 清理
- 失败 → 记录原因（退出码/报错/API 错误码）
- ⚠️ Real-Environment First——不使用 mock 模式

### 1.x 变更类操作自动执行
- 变更类操作（Create/Update/Delete）自动执行，不需用户确认
- **变更类测试执行顺序**：同一资源的变更类操作按生命周期顺序执行——先 create 拿到真实 ID，再用这个 ID 执行 get/update/delete。test-vars.json 里 update/delete 命令的 `--id 1` 是示例值，tester 执行时用 create 返回的真实 ID 替换
- 自动资源生命周期测试：Create → get 验证（用 create 返回的 ID）→ update（用 create 返回的 ID）→ delete 清理（用 create 返回的 ID）
- 文件上传测试前自动创建临时测试文件（如 `echo "test" > /tmp/test-upload.txt`），测试完成后清理
- 只读查询类（List/Get）→ 自动真实执行
- cleanup 执行：test-vars.json 中 `cleanup` 字段为描述性文本（如"执行 delete-article 删除刚创建的测试资源"）。
  agent 执行 create 命令后从返回 JSON 中提取资源 ID（递归查找——先看顶层 id/article_id/aid 等字段，
  再看嵌套的 data.id/data.article_id 等），自行拼接 cleanup 命令并执行。
  如果返回 JSON 中找不到 ID 字段——记录警告（⚠️ cleanup 未执行：返回结果无 ID 字段），不算测试失败

### 2. 规范检查（脚本化）

跑 `python3 {{SKILL_DIR}}/rest-api-skill-tester/scripts/spec_check.py <skill_dir>`
- 检查 frontmatter / 文件结构 / version 格式 / mock 模式（严禁）
- 检查 SKILL.md 段落规范：必填（场景/知识/步骤）+ 可选（判断标准/输出规范/参考文档）——对齐 `references/skill-spec.md` 规范
- 必填段落缺失 = spec_check 失败
- 段落名匹配中英文别名：场景/When to Use、知识/Knowledge、步骤/Steps

### 3. 安全扫描（gitleaks）

跑 `gitleaks detect --source <skill_dir> --no-git`（若已装）
- 未装 → 降级人工检查
- 发现密钥 → 🔴 失败

### 4. 报告（Issue 评论 + PR 评论）

报告格式见 `references/test-report-format.md`。

⚠️ **测试完成后必须发 Issue 评论 + PR 评论**：

Issue 评论：

```bash
curl -s --max-time 30 -X POST \
  -H "PRIVATE-TOKEN: $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "[$AGENT_NAME] ✅ 测试通过\n\n## Skill 测试报告：{name} v{version}\n\n## 功能验证\n| 检查项 | 状态 | 证据 | 说明 |\n|--------|------|------|------|\n| API 调用 | ✅ | 返回 N 条数据 | 真实调用成功 |\n\n## 结论\n✅ 测试通过，可合并发布"}' \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/comments"
```

PR 评论：

```bash
curl -s --max-time 30 -X POST \
  -H "PRIVATE-TOKEN: $BOT_GITCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "[$AGENT_NAME] ✅ 测试通过"}' \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/pulls/$PR_NUMBER/comments"
```

### Step 5: 合并 + A2A 交棒（测试通过后）

所有 skill 测试通过后统一自动合并（mode=merge）。

```bash
bash {{SKILL_DIR}}/rest-api-skill-tester/scripts/merge_skill_pr.sh $PR_NUMBER
```

### MERGED=1 → A2A 交棒 SRE Agent

```bash
curl -s --max-time 30 -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "sre",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "issue_number": "'"$ISSUE_NUMBER"'",
      "pr_number": "'"$PR_NUMBER"'",
      "context": "测试通过，已自动合并。Skill：{name}，请发布到 ClawHub/GitHub/GitCode"
    }
  }'
```

### MERGED=0 → 自动合并失败

- 评论 PR + Issue：「✅ 测试通过，自动合并失败，请手动合并」

### 测试失败 → A2A 回 Coding Agent

```bash
curl -s --max-time 30 -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "coding",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "issue_number": "'"$ISSUE_NUMBER"'",
      "pr_number": "'"$PR_NUMBER"'",
      "context": "测试失败：{原因摘要}。请修复后重新 push"
    }
  }'
```

## 判断标准 / Verification

- 功能验证（只读类）：API 调用返回有效数据（非空/字段完整/退出码 0）= 通过
- 功能验证（变更类）：Create → get → update → delete 生命周期测试成功 + 资源已清理 = 通过
- 规范检查：spec_check.py 退出码 0（含段落规范：场景/知识/步骤）= 通过
- 安全扫描：gitleaks 无密钥发现 = 通过
- 以上全部通过 = 测试通过 → 调合并脚本 → A2A SRE Agent
- 任一项失败 = 测试失败 → A2A 回 Coding Agent

## 输出规范 / Output Format

- 评论 body 必须以固定标记开头（保证可检索性）
- 报告含证据表格（检查项/状态/证据/影响/修复建议）

### 分析结论示例

成功时：

```
分析结论：
- 场景：功能验证（QA Agent 被 Coding Agent A2A 触发）
- Skill：{skill_name} v{version}
- 测试结果：✅ 通过（API 调用成功，返回 N 条数据）
- 必须执行：报告 Issue+PR 评论 → 调合并脚本 → A2A SRE Agent
```

失败时：

```
分析结论：
- 场景：功能验证
- Skill：{skill_name} v{version}
- 测试结果：🔴 失败（API 调用超时 / spec_check 失败）
- 必须执行：报告 Issue+PR 评论 → A2A 回 Coding Agent
```

## 参考 / References

- 测试报告格式，读取 `references/test-report-format.md`
- 调用 GitCode API 时，必须以 `references/gitcode-api-reference.md` 为准

## 任务结束

```json
{
  "actions": ["做了什么，如 tested:rest-api-skill-xxx / merged:PR#N / a2a:sre / a2a:coding"],
  "conclusion": "处理结论（测试通过已自动合并/测试失败/自动合并失败）",
  "artifacts": ["产生的产物：测试报告评论 / PR 评论 / 合并状态"],
  "next_step": "下一步建议",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```
