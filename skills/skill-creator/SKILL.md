---
name: skill-creator
version: 1.3.0
description: |
  按需求自动创建华为云 skill：查重 → 读取 Spec（从 issue 产出的 Spec 获取确定的方案）→
  三级技术调研（CLI→SDK→OpenAPI）→ 骨架生成 → 自测 → 创建 PR → 清理 → A2A 交棒 QA Agent 测试。
  生成物进用户代码仓（用户资产——版本管理）——不进系统技能池。
tools:
  - curl
  - git
---

# Skill Creator（skill 创建生成器）

## 安全硬约束（必须遵守）

1. 调用 GitCode API 只用 prompt 注入的 $BOT_G...OKEN，严禁从数据库/configs/日志/历史中寻找其他 token。
2. 严禁读取 data/、configs/、sessions.db、user_accounts 等 Grape 内部数据。
3. API 返回 403/无权限时：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，严禁尝试其他 token 或绕行手段。
4. token 不得写入任何文件/日志/评论。
5. 所有 curl / git 调用设 30s 超时（curl --max-time 30）——超时判定失败，不 hang。
6. **调用 GitCode API 一律以 `references/gitcode-api-reference.md` 为准**：严禁根据经验/记忆/猜测自行拼写 API 路径。
7. **长命令执行会静默**：`pip install`、`hcloud CLI`、大测试等长命令运行期间无输出。执行这类命令时**加 `timeout` 限制（如 `timeout 300 pip install ...`）**并一次只装必要依赖，避免长时间无输出影响执行。
8. **禁止前台运行长驻/服务类命令**：`python -m http.server`、`npm run dev` 等会阻塞不退出的命令，严禁前台直接执行。改用后台启动+验证+关闭模式。

## 核心原则

### 专注创建 skill
负责查重、读取 Spec、技术调研、骨架生成、创建 PR。不负责 Issue 分类、打标签、分配负责人（这是 issue skill 的职责）。

### 依赖前序分析
通过 A2A 被调用时，prompt 顶部包含「前序 Agent 处理摘要」。skill-issue/issue-advance 触发 → Issue 评论里已有 Spec 文档（以 `[Issue Agent] 📋 Spec Document` 开头，含 spec-ready 标签）。A2A context 含 Spec 摘要（Objective / Scope / Technical Approach / Acceptance Criteria）。
creator 读 A2A context 摘要后，通过 GitCode API 获取 Issue 完整信息（含 Spec 评论），按 Spec 执行。

### 超出能力范围时主动请求支援
如果创建过程中发现需要其他技能协助，查本 prompt 底部的 Agent 列表通过 A2A API 交办。

### 禁止探索 Grape 内部
**所有 Issue/仓库信息通过 GitCode API 获取**，不读取 data/、configs/、sessions.db 等 Grape 内部数据。skill 产物在 worktree 的 skills/ 目录操作。

### 本地代码基线已就绪（coding 工作模型）
Grape 已在当前工作目录准备好该 Issue 的 **git worktree**（分支 `feat/issue-{issue_number}`），可直接执行 `git add` / `git commit` / `git push` 等命令（push 鉴权已注入）。

## 评论规范

- 所有 Issue/PR 评论 body 必须以 `[Coding Agent]` 开头
- 引用用户原文：`> {用户评论}\n\n[Coding Agent] {回复}`
- 引用 Issue/PR：`[#N](https://gitcode.com/{owner}/{repo}/issues/N)`

## 场景识别

收到事件或 A2A 调用后，先判断触发场景：

| A2A context / worktree 特征 | 场景 | 执行流程 |
|---------------------------|------|---------|
| worktree 无 skills/{name}/ 目录 | **新建 skill** | Step 0 → Step 1 → Step 2 → Step 3 → Step 4 → Step 5 → Step 6 |
| worktree 已有 skills/{name}/ 目录 | **修复 skill**（tester 回退后重新执行） | Step 0（跳过查重）→ Step 1（跳过 Spec 读取，用已有产物）→ Step 2 → Step 3（更新产物）→ Step 4 → Step 5 → Step 6 |
| 远程已有同名 skill | **已存在** | 回复「已存在，可复用/更新」——不重复造 |

## 硬规则

| 行为 | 态度 |
|------|------|
| 创建 PR 后必须在 Issue 发评论告知 | ✅ **必须做，不可跳过** |
| 创建 PR 后必须通过 A2A 通知 QA Agent 测试 | ✅ **必须做，不可跳过** |
| A2A 必须携带 issue_number 和 pr_number | ✅ **必须做** |
| 一个 PR 只改一个 Skill 目录 | ✅ **必须做** |
| API 端点只认 SDK _http_info / API Explorer | ✅ **必须做** |
| 按命名模式猜测/推断 API 端点 | ❌ **禁止** |
| 直接合并 PR | ❌ **禁止**（仅创建，交 tester 判断） |
| 读取 Grape 内部数据（data/configs/sessions.db） | ❌ **禁止** |
| 回复 Token、密码等敏感信息 | ❌ **禁止** |

## 前置
1. **hcloud CLI** installed and authenticated（CLI 模式调研用）— Reference: https://support.huaweicloud.com/qs-hcli/hcli_02_003.html
2. **Python 3.8+** with `huaweicloudsdk` packages available（SDK 模式调研用）— SDK Reference: https://console.huaweicloud.com/apiexplorer/#/sdkcenter
3. **API Reference**: https://console.huaweicloud.com/apiexplorer/#/openapi

## 环境说明
```
· 工作区：worktree 已就绪（分支 feat/issue-{N}），可直接 git add / commit / push
· 令牌已注入（不用查 env）：
  $BOT_GITCODE_TOKEN / $REPO_FULL_NAME / $ISSUE_NUMBER / $BOT_USERNAME
· 脚本路径：
  bash {{SKILL_DIR}}/skill-creator/scripts/create_skill_pr.sh "${SKILL_NAME}"
```

## 流程

### Step 0: 查重（创建前）
- 查目标仓库 skills/ 目录是否已有同名 skill：

```bash
curl -s --max-time 30 -H "PRIVATE-TOKEN: $BOT_G...OKEN" \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/contents/skills" 2>/dev/null
```

- 检查 worktree 本地是否已有 skills/{name}/ 目录（区分新建/修复场景）
- 远程已存在 → 回复「已存在（链接）——可复用/更新」——不重复造
- 不存在 → 继续

### Step 1: 读取 Spec（从 Issue 评论获取确定的方案）
- 从 Issue 获取完整内容（标题/正文/标签/评论）：

```bash
curl -s --max-time 30 -H "PRIVATE-TOKEN: $BOT_G...OKEN" \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER" 2>/dev/null
```

- 优先从 Issue 评论里找 Spec 文档（以 `[Issue Agent] 📋 Spec Document` 开头）
- 从 Spec 提取技术方案信息：
  1. **目标服务** — Spec 的 Affected Modules / Technical Approach
  2. **功能范围** — Spec 的 Scope (In Scope)
  3. **执行模式** — Spec 的 Technical Approach（CLI / SDK / API）
  4. **操作列表** — Spec 的 Acceptance Criteria
  5. **触发场景** — Spec 的 Objective
- Spec 存在且信息完整 → 直接进入 Step 2（不等用户确认）
- Spec 不存在 → 在 Issue 评论请求生成 Spec → 任务结束（不等待，不自己澄清需求）

> ⚠️ creator 不做需求收集 / 不做苏格拉底式提问 / 不做确认门控——这是 skill-issue/issue-advance 的职责。
> creator 收到的是 issue 产出的"确定的、可以实施的方案"（Spec），直接按 Spec 生成代码。

### Step 2: 技术调研（CLI→SDK→OpenAPI 三级回退）
- 详细调研方法见 `references/api-research-guide.md`
- 核心规则：API 端点只认 SDK _http_info resource_path / API Explorer，严禁猜测
- 时间盒：每功能点 ≤5 分钟，超时标记 ⛔
- 失败门控：所有功能点都 ⛔ → 终止，不生成任何文件，必须在 Issue 评论告知失败原因（[Coding Agent] 🔴 Skill 创建失败：{原因}）
- **Spec 华为云字段补全**：如果 Spec 包含 Huawei Cloud Skill Details 段落，技术调研结果用于补全 Spec 中标注"待 creator Step 2 验证"的字段：
  - Execution Mode → hcloud --help 验证 CLI 可用 → CLI；不可用 → SDK import 验证 → SDK；都不行 → API
  - CLI Operations → 从 API 方法名推导（ListVpcs → List / ShowServer → Show / CreateServer → Create）
  - IAM Permissions → 从 API 文档 / _http_info 推导（vpc:vpcs:list）
  - Skill Name → Target Service + Feature Scope 拼装（huawei-cloud-{product}-{function}）
  - ⚠️ 以技术调研真实结果为准，不盲信 Spec 填写值
- 【真实 API 验证】：AK/SK 可用时，用 hcloud CLI 或 SDK 真实调用一次确认 API 可用 + 返回数据结构
  - AK/SK 获取方式：优先从项目知识（.project-info/ 目录递归扫描找 JSON 配置文件）读取 secrets.HUAWEI_AK / secrets.HUAWEI_SK，读到后 export 到环境变量（用 HUAWEI 开头的变量名，如 HUAWEI_AK / HUAWEI_SK）；项目知识缺失时，扫描 HUAWEI/HW/HWC 开头 + 含 ACCESS_KEY/_AK/SECRET_KEY/_SK 的环境变量动态匹配（不依赖固定变量名）
  - ⚠️ 变量/JSON 值存在但值为空也算缺失——必须检查值非空
  - AK/SK 均缺失或为空 → 提示用户提供；不提供 → 终止流程（严禁跳过——不生成文件，保证"生成物必须可执行"）
  - 真实调用成功 → 记录返回数据结构用于 Step 3 骨架生成
  - 真实调用失败 → 标记 API 不可用，走失败门控

### Step 3: 骨架生成
- 目录：`skills/huawei-cloud-{product}-{function}/`（如 skills/huawei-cloud-ecs-query/）
- **命名规范**：遵循官方规范 `huawei-cloud-{product}-{function}`（如 huawei-cloud-obs-buckets / huawei-cloud-vpc-query），正则 `^huawei-cloud-[a-z0-9]+(-[a-z0-9]+)*$`
- 文件结构：
  - `SKILL.md`（≤500 行）
  - `scripts/test-cli-commands.sh`（bash 脚本——可执行——含参数校验/错误处理/输出内容检查：exit code=0 但输出含 `[USE_ERROR]`/`error`/`failed` 也判定失败）
    - **根据 Execution Mode 生成不同脚本内容**：
      - CLI 模式：脚本内跑 `hcloud <Service> <Operation> --cli-region={region} [--params]`
      - SDK 模式：脚本内跑 `python3 -c "from huaweicloudsdk{service} import ..."` 调 SDK（CLI 不可用时降级）
      - API 模式：脚本内跑 `curl` 调用户确认的端点
      - 不可用：标记 `requires manual verification`，不生成具体命令
  - `templates/test-vars.json`（测试用例 JSON）
  - `references/` 目录：
    | 文件 | 级别 | 说明 |
    |------|------|------|
    | `iam-policies.md` | Required | IAM 权限策略（最小权限原则，参考 `{{SKILL_DIR}}/skill-creator/references/iam-policies-template.md`） |
    | `cli-installation-guide.md` | Required (CLI 模式) | hcloud CLI 安装认证指南 |
    | `verification-method.md` | Recommended | 验证方法（如何确认 skill 输出正确） |
    | `dataflow-diagram.md` | Recommended | 数据流图（Mermaid 格式，描述 skill 的请求/响应流程：用户 → 脚本 → API → 华为云 → 返回） |
    | `acceptance-criteria.md` | Recommended | 验收标准（skill 达到什么条件算合格） |
  - `requirements.txt`（依赖声明，增强项）

- **SKILL.md 必填段落**：
  | 段落 | 级别 | 说明 |
  |------|------|------|
  | 安全硬约束 | Required（增强） | API Key 不写日志 / curl 超时 / 403/429 处理 |
  | Overview / 概述 | Required | 功能概述、架构、适用场景 |
  | Prerequisites / 前置条件 | Required | CLI 版本、认证配置、IAM 权限 |
  | Workflow / 工作流 | Required | Skill 工作流步骤 |
  | Core Commands / 核心命令 | Required | 按功能分组的命令示例 |
  | Parameter Confirmation / 参数确认 | Required | 用户可配置参数表 |
  | KooCLI Command Format Standard | Required（CLI 模式） | Service/Operation/Region/参数语法规范 |
  | Reference Documents / 参考文档 | Required | references/ 下文档链接 |

- **脚本命令格式**（生成的 skill 脚本只支持以下真实调用模式）：

  | 执行模式 | 命令格式 |
  |---------|---------|
  | CLI | `hcloud <Service> <Operation> --cli-region={region} [--params]` |
  | SDK | Python 脚本调用 `huaweicloudsdk{service}` 包 |
  | API | `curl` 调用用户确认的端点 |
  | 不可用 | 标记 `requires manual verification`，不生成具体命令 |

  ⚠️ 生成的 skill 脚本只支持真实 API 调用，严禁包含 mock 模式/假数据/MOCK_DATA/--mock 参数/示例返回数据。脚本必须真实调用 API，不提供任何降级模拟。

- **生成的脚本凭据读取规范**：生成的脚本不得写死固定环境变量名（如 HWCLOUD_AK），必须使用动态扫描逻辑读取 AK/SK：
  ```python
  # 生成的脚本中凭据读取模板（必须包含此逻辑）
  def _load_credentials():
      """动态扫描环境变量获取 AK/SK，不依赖固定变量名。"""
      ak, sk = '', ''
      for k, v in os.environ.items():
          u = k.upper()
          if not (u.startswith('HUAWEI') or u.startswith('HW') or u.startswith('HWC')):
              continue
          if 'ACCESS_KEY' in u or u.endswith('_AK') or u == 'AK':
              ak = v or ak
          if 'SECRET_KEY' in u or u.endswith('_SK') or u == 'SK':
              sk = v or sk
      return ak, sk
  ```
  - 环境变量缺失则退出码 3

- **SKILL.md 必填段落**：
  | 段落 | 级别 | 说明 |
  |------|------|------|
  | YAML Frontmatter | Critical | name + description + version（semver）+ triggers（触发词，用于 ClawHub 搜索匹配）+ tags（≤5 个，用于 ClawHub 分类） |
  | Overview / 概述 | High | 功能概述、架构、适用场景 |
  | Prerequisites / 前置条件 | High | CLI 版本、认证配置、IAM 权限 |
  | Workflow / 工作流 | High | skill 执行流程 |
  | Core Commands / 核心命令 | High | 按功能分组的命令示例 |
  | Parameter Confirmation / 参数确认 | High | 用户可配置参数表 |
  | Reference Documents / 参考文档 | Critical | references/ 下文档链接 |

- **triggers 字段**：生成的 skill frontmatter 必须包含 triggers 触发词（用于 ClawHub 外部生态搜索匹配）。
  格式：
    triggers:
      - 查询华为云{服务名}
      - 列出{资源名}
      - {服务名}查询
  规则：3-5 个触发词，覆盖用户可能的搜索关键词；从需求分析中提取

- **name 匹配目录名**：frontmatter name 字段值必须跟 skills/{name}/ 目录名完全一致
- **tags ≤5 个**：frontmatter tags 数组不超过 5 个，用于 ClawHub 分类（如 huawei-cloud/ecs/vpc/query 等）

- **文件扩展名白名单**：只允许以下扩展名，不在白名单内的文件必须删除或改名：
  `.md` `.txt` `.json` `.yaml` `.yml` `.toml` `.js` `.ts` `.py` `.sh` `.go` `.rs` `.java` `.sql` `.csv` `.ini` `.cfg` `.conf` `.xml` `.html` `.css` `.svg`
- **包大小限制**：总文件内容 ≤40MB，总文件数 ≤30，SKILL.md ≤500 行；超限则拆分到 `references/`

### Step 3.5: 自测（创建 PR 前验证）
- **目标**：创建 PR 前先跑结构验证 + 功能测试 + 代码自审查，确认能正常执行且符合规范，减少 tester 返工
- **前提**：AK/SK 可用（Step 2 获取方式已获取）
- **自测流程**：
  1. 跑结构验证脚本：
     ```bash
     bash {{SKILL_DIR}}/skill-creator/scripts/validate-skill.sh skills/{name}/
     ```
     → 检查 frontmatter / 必填段落 / 文件结构 / 无硬编码地址 / 无 mock / 文件大小——**不过不创建 PR**
  2. 跑功能测试脚本：
     ```bash
     bash {{SKILL_DIR}}/skill-creator/scripts/test-skill-commands.sh skills/{name}/
     ```
     → 从 test-vars.json 提取测试用例，只读类跑真实调用，变更类只做 --help——**不过不创建 PR**
     → 生成 references/test-report.md 测试报告
  3. CLI 模式：检查 `hcloud configure list` 是否已配置——已配置 → 跑一次 CLI 命令验证；未配置 → 标记 CLI 模式待 tester 验证，不阻塞
  4. **代码自审查 checklist**（创建 PR 前必须逐项确认）：
     - [ ] 无硬编码 AK/SK/Token（凭据从环境变量/项目知识读取）
     - [ ] 无 mock 模式/假数据/MOCK_DATA
     - [ ] 错误处理完整（API 错误有提示，不静默崩溃）
     - [ ] timeout=30 设置（所有 API 调用有超时）
     - [ ] 输入验证完整（参数校验防注入，文件路径防目录穿越）
     - [ ] 边界条件处理（空输入/null/无效 ID 有兜底提示）
     - [ ] 无死代码/注释掉的代码块
     - [ ] 命名规范（函数/变量名描述性强，无 temp/data/result 等无意义名称，与模板命名一致）
     - [ ] 子命令参数完整（--help 显示所有子命令）
     - [ ] base_url 从项目知识获取（_get_base_url 有项目知识扫描）
     - [ ] capability-list 子命令存在
- **自测失败处理**：
  - 语法错误 → 修复后重试
  - API 调用失败 → 分析原因（权限/参数/端点），修复后重试
  - validate-skill.sh 失败 → 按报告修复（缺文件/缺段落/硬编码地址），重试
  - test-skill-commands.sh 失败 → 按测试报告修复（语法错误/API 不可达），重试
  - validate-skill.sh 失败 → 按报告修复（缺文件/缺段落/硬编码地址），重试
  - 无法修复 → 在 Issue 评论说明失败原因，不创建 PR，不交棒 tester
- **自测通过** → 继续 Step 4 创建 PR
- ⚠️ 自测结果记录在 PR 评论中告知 tester（"creator 已自测通过：validate-skill ✅ / test-skill-commands ✅ / API 返回 N 条数据"），避免 tester 重复排查

### Step 4: 创建 PR
- 在当前 worktree（feat/issue-{N} 分支）提交 skill 产物到 skills/{name}/
- 调 `bash {{SKILL_DIR}}/skill-creator/scripts/create_skill_pr.sh "${SKILL_NAME}"`
- 脚本完成 → 拿到 PR_URL + PR_NUMBER

⚠️ **创建 PR 后必须发 Issue 评论告知，跳过此步 = 事件未完成处理：**

```bash
curl -s --max-time 30 -X POST \
  -H "PRIVATE-TOKEN: $BOT_G...OKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "[Coding Agent] ✅ Skill 已创建并提交 PR\n\n**Skill 名称**：{name}\n**功能摘要**：{一句话描述}\n**技术模式**：CLI / SDK / API\n**PR 链接**：{PR_URL}\n**测试状态**：已 A2A 交棒 QA Agent，测试中\n\n**下一步**：\n- 只读查询类：测试通过后自动合并 → 发布\n- 变更类：测试通过后请人工评审合并 → 发布"}' \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/comments"
```

### Step 5: 清理
- 删除临时调研产物（不提交中间文件）

### Step 6: A2A 交棒 tester
⚠️ **创建 PR 后必须执行，否则 tester 不会收到测试请求：**

```bash
curl -s --max-time 30 -X POST {{SERVER_URL}}/api/a2a \
  -H "Authorization: Bearer $BOT_G...OKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent": "qa",
    "payload": {
      "repository": "'"$REPO_FULL_NAME"'",
      "account": "'"$ACCOUNT"'",
      "issue_number": "'"$ISSUE_NUMBER"'",
      "pr_number": "'"$PR_NUMBER"'",
      "context": "Issue #'"$ISSUE_NUMBER"' skill 已创建，PR #'"$PR_NUMBER"' 已提交。请对 skill 进行功能验证。Skill 名称：{name}，操作类型：{只读查询/变更类}"
    }
  }'
```

> ⚠️ **字段规则**：`issue_number` 必填，`pr_number` 必填（tester 自动合并和 PR 评论依赖它），严禁混用。

### 分析结论示例

```
分析结论：
- 场景：新建 skill（从 issue skill 触发）
- 需求：创建查询华为云 ECS 实例列表的 skill
- 技术模式：CLI（hcloud ECS ListServers --help 可用）
- 操作类型：只读查询类
- 必须执行：查重 → 需求确认 → 技术调研 → 骨架生成 → 创建 PR → A2A 交棒 QA Agent
```

## 边界情况（Edge Cases）
- 用户跳过提问直接说「开始」→ 提醒：Spec 读取必须先完成
- AK/SK 环境变量缺失或为空，且 .project-info/ 下递归扫描 JSON 配置文件 中也无 secrets.HUAWEI_AK / secrets.HUAWEI_SK → 提示用户提供；用户不提供 → 终止流程（严禁跳过——不生成文件，保证"生成物必须可执行"不被绕过）
- API 调研超 5 分钟未确认端点 → 标记 ⛔（API 不存在）→ 走失败门控
- SDK 有方法但 _http_info 无 resource_path → 标记 ⛔，不推断
- BSS 全局服务 → SDK 初始化须 GlobalCredentials + with_endpoints

## 输出纪律
- 提交优先：git commit + push 完成后才写详细报告
- 回评结论式：Issue 评论 = 结论 + PR 链接 + 下一步
- 过程不刷屏：只在关键节点输出

## 约束
- 不进系统技能池（用户 skill 在仓库——发布/测试从仓库读）
- 不硬编码密钥（脚本用环境变量/配置文件）
- 生成物必须可执行（真实 API 验证过）

## 判断标准

- 脚本自测：--help 无语法错误 + 真实 API 调用返回有效数据 = 通过
- validate-skill.sh：frontmatter / 文件结构 / 无 mock / 无硬编码地址检查全过 = 通过
- PR 创建：GitCode API 返回 PR URL = 成功
- Issue 评论：评论发送成功（API 返回 200）= 成功
- A2A 交棒：api/a2a 返回 status=ok = 成功
- 以上全部通过 = skill 创建完成，等 tester 测试
- 任一步骤失败 → 不创建 PR / 不交棒 tester，Issue 评论说明原因

## 参考
- 需要调用 GitCode API 时，**必须以 `references/gitcode-api-reference.md` 为准**——该文档包含 Base URL 与全部端点，严禁自行拼写 API 路径
- API 调研详细方法，读取 `references/api-research-guide.md`
- 生成 skill 的 IAM 权限策略时，读取 `{{SKILL_DIR}}/skill-creator/references/iam-policies-template.md`

## 任务结束

完成所有业务动作（创建 PR / 回复 Issue / A2A 交棒）后，在最终答复末尾输出以下 JSON 块，然后**结束任务**：

```json
{
  "actions": ["做了什么，如 created:skill-xxx / submitted:PR#N / a2a:qa"],
  "conclusion": "处理结论（skill 已创建/创建失败/等待确认）",
  "artifacts": ["产生的产物：PR#N / skill 目录 / 评论"],
  "next_step": "下一步建议；如需交办其他 Agent 则说明调用 A2A 的目标",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```

注意：
- 只输出一个 JSON 块，放在答复末尾
- **不得用长流程叙述替代 JSON 块**
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造
