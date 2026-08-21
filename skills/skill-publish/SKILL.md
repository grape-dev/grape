---
name: skill-publish
version: 1.3.0
description: |
  将 skill 发布/下架到外部平台（ClawHub/GitHub/GitCode）：
  幂等探测 → bump 重试（≤3 次）→ 多平台发布/下架 → 校验 → 汇总回复。
  ClawHub 支持组织发布（ownerHandle，可选）；403/404/401 严格区分，防止误判。
tools:
  - curl
  - git
---

# Skill 发布管理

## 安全硬约束（违反即违规）
1. **令牌获取方式**：优先读环境变量 `$CLAWHUB_TOKEN` / `$GITHUB_TOKEN` / `$GITCODE_TOKEN` / `$CLAWHUB_OWNER_HANDLE`；环境变量缺失时，递归扫描 .project-info/ 目录找 repo-credentials.json，从 secrets 字段读令牌（CLAWHUB_TOKEN / GITHUB_TOKEN / GITCODE_TOKEN），从 config 字段读发布配置（GITHUB_PUBLISH_REPO / GITCODE_PUBLISH_REPO / CLAWHUB_OWNER_HANDLE），读到后 export 到环境变量
2. **禁止直连业务数据库**（repo_platform_tokens 等）、读取 Grape 运行目录（data/、configs/、sessions.db）或日志寻找 token；允许的 token 来源仅限：环境变量 + .project-info/ 下 repo-credentials.json
3. **严禁遍历系统目录考古**（/usr/local/lib/hermes-agent、/opt/grape 等）——只读注入的环境变量或项目知识库 repo-credentials.json
4. **401/403 区分处理**：
   - 401 = 令牌无效/过期 → 报告「{平台} 令牌无效或过期，请到设置页更新」→ 跳过该平台
   - 403 = 令牌无权限 → 报告「{平台} 令牌对目标无权限，请检查 token 权限或目标归属」→ 跳过该平台
   - 两者都**立即停止该平台任务，不重试、不用其他 token 绕行**
5. token 绝不写入任何文件、日志、评论、脚本（项目知识库 JSON 由上层业务维护，本技能只读不写）
6. 所有 curl / git 调用设 30s 超时（curl --max-time 30；git 用 timeout 30）——超时判定该平台任务失败，不 hang
7. **调用 GitCode API 一律以 `references/gitcode-api-reference.md` 为准**：严禁根据经验/记忆/猜测自行拼写 API 路径

## 评论规范

- 所有 Issue/PR 评论 body 必须以 `[SRE Agent]` 开头
- 引用用户原文：`> {用户评论}\n\n[SRE Agent] {回复}`

## 硬规则

| 行为 | 态度 |
|------|------|
| 令牌优先读环境变量，缺失时回退 repo-credentials.json | ✅ **必须做** |
| 发布/下架完成后必须发 Issue 评论告知 | ✅ **必须做，不可跳过** |
| 一个平台失败不中断其他平台 | ✅ **必须做** |
| 查询数据库/配置文件/日志寻找 token | ❌ **禁止** |
| 遍历系统目录考古（/usr/local/lib、/opt/grape 等） | ❌ **禁止** |
| token 写入文件/日志/评论/脚本 | ❌ **禁止** |
| 401/403 时不停止用其他 token 绕行 | ❌ **禁止** |

## 核心原则

### 专注发布
负责将已创建并测试通过的 skill 发布到外部平台。不负责创建 skill、测试 skill（那是 Coding Agent/QA Agent 的职责）。

### 依赖前序分析
通过 A2A 被调用时，prompt 包含 tester 的处理摘要（skill 名称/PR 编号/合并状态）。skill 产物在当前 worktree 的 skills/{name}/ 目录。

### 禁止探索 Grape 内部
**令牌只从环境变量读取**，不读取 data/、configs/、sessions.db 等 Grape 内部数据。

## 入口判定
- 同步/发布/下架/撤回 skill 请求 → 本技能
- 非 skill 发布类请求 → 不处理，回复说明范围

## 定位 skill
- 仓库内 skills/{name}/ 或根目录 {name}/（含 SKILL.md）
- version 取 SKILL.md frontmatter（默认 0.1.0）；slug = name 转小写连字符
- **文件指纹 SHA-256**：skill 目录全部文件**按文件路径字典序排序**后读取拼接，计算哈希——用于幂等比对（排序保证多次计算一致）。排除 .git/、__pycache__/、*.pyc——这些不是 skill 内容，包含会导致指纹不一致

## ClawHub OwnerHandle（组织发布）
1. ownerHandle 来源优先级：**用户指令指定 > 环境变量 $CLAWHUB_OWNER_HANDLE > 空（个人账号发布）**
2. SKILL.md 不存储 ownerHandle——归属完全由 API 入参控制
3. 探测 GET / 下架 DELETE 必须带 `?ownerHandle={ownerHandle}`（配置了组织时）——不带只查个人命名空间，会误判组织 skill 不存在（404）
4. POST payload 可选字段 ownerHandle：非空 = 发布到指定组织；token 必须具备该组织 publisher 角色，否则 403
5. **不支持变更已有 skill 归属**（个人→组织迁移官方有 bug）；slug 在同一个 publisher 下唯一
6. 组织 skill 对外完整引用格式：`@{ownerHandle}/{slug}`；个人：`{slug}`
7. 回复安装命令：有 owner → `clawhub install @{owner}/{slug}`；无 → `clawhub install {slug}`
8. **ClawHub 页面 URL 格式**（已验证）：`https://clawhub.ai/{ownerHandle}/skills/{slug}`
   - 个人发布：`https://clawhub.ai/{用户名}/skills/{slug}`（如 `https://clawhub.ai/yangaiwu/skills/hcs-vpc-details`）
   - 组织发布：`https://clawhub.ai/{org}/skills/{slug}`
   - ⚠️ **不是** `https://clawhub.ai/skills/{slug}`（会 307 重定向到 `/skills/skills/{slug}`，页面空 skeleton）
   - ⚠️ **不是** `https://clawhub.ai/{用户名}/{slug}`（少了 `/skills/` 段，307 重定向）
   - **ownerHandle 获取优先级**：
     a. 发布时使用的 ownerHandle（用户指令/环境变量 $CLAWHUB_OWNER_HANDLE/repo-credentials.json config）
     b. GET /api/v1/skills/{slug} 返回的 `owner.handle`（发布后校验时获取）
     c. GET /api/v1/whoami 返回的 `user.handle`（个人发布兜底——用 Bearer $CLAWHUB_TOKEN 调，返回当前账号 handle）
   - 拼好 URL 后必须 GET /api/v1/skills/{slug} 确认 skill 存在（200=URL 正确，404=发布失败）
   - 不得自己拼 URL——必须用上述优先级获取 ownerHandle

## 发布流程
1. 定位 skill + 读 frontmatter（name/version/description）
2. 读取各平台令牌（优先环境变量，缺失时回退 .project-info/ 下 repo-credentials.json）：
   - 环境变量和 JSON 均缺失 → 报告「未配置 {平台} 令牌，请到设置页配置」→ 跳过该平台
   - 401/403 → 按安全约束区分报告 → 跳过该平台（不中断其他平台）
3. 幂等探测:
   - 计算 skill 文件指纹 SHA-256（字典序排序拼接）
   - ClawHub: GET /api/v1/skills/{slug}（组织配置时带 ?ownerHandle=）——200（skill 存在）→ 比对版本/指纹——内容未变 → 跳过（报告「已是最新，无需发布」）——变了 → bump 后发布——404（skill 不存在：从未发布或已删除）→ 直接发布，不跳过
   - GitHub/GitCode: git ls-remote 目标仓库 skills/{name}/ 是否已存在——已存在且内容一致 → 跳过——不同 → 覆盖 push
4. 版本 bump 规则:
   - patch 段两位数上限 99 → 进位（1.0.99 → 1.1.0）
   - "already exists" / 版本冲突 → bump 重试；**最大 3 次，超限直接失败终止，禁止无限循环**
   - POST 返回 "slug already exists"（非版本冲突，是 slug 级别占用）→ 报告「skill slug 被软删除占用，请等待 30 天或联系 ClawHub 支持」→ 该平台失败，不重试
   - ⚠️ bump 仅本次执行内存生效，**不回写 SKILL.md**（版本号下次发布时从 frontmatter 重新读取；如需持久版本请上层业务维护）
5. 按平台发布:
   - ClawHub: POST https://clawhub.ai/api/v1/skills（multipart payload+files，Bearer $CLAWHUB_TOKEN，payload 可带 ownerHandle）
   - GitHub/GitCode: 调脚本封装 clone→备份→覆盖→1次commit→push：
     ```bash
     bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh \
       --action publish --platform github \
       --skill-name {name} --skill-dir {skill_dir} --version {version}
     bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh \
       --action publish --platform gitcode \
       --skill-name {name} --skill-dir {skill_dir} --version {version}
     ```
     脚本输出 PUSHED=1/0 + BACKUP_BRANCH + TARGET_URL；一个平台失败不中断其他平台
6. 脚本内置安全备份：目录已存在时自动建 backup/pre-sync-{timestamp} 分支并推远端（无需手动执行）。备份分支推送失败不阻断主流程——发布结果不受影响
7. 校验: SHA256 逐字节比对 + 平台 API 确认状态；ClawHub 校验时从 GET /api/v1/skills/{slug} 返回的 `owner.handle` 拼接页面 URL（`https://clawhub.ai/{owner.handle}/skills/{slug}`）
8. 目标仓库解析（GitHub/GitCode）:
   - 默认读 $GITCODE_PUBLISH_REPO / $GITHUB_PUBLISH_REPO（格式 namespace/repo）
   - 用户指令指定目标仓库 → **只接受 namespace/repo 格式，拒绝完整 git URL**；用户指令优先级高于环境变量
   - 未配置 → 报告「未配置发布目标仓库，请到设置页配置」→ 跳过该平台
   - ⚠️ **跳过平台时必须说明真实原因，不得用「未配置」兜底**：
     - 令牌缺失（环境变量和 JSON 都没有）→ 「未配置 {平台} 令牌」
     - 令牌存在但无权限（403）→ 「{平台} 令牌无写入权限」
     - 仓库不存在（404）→ 「目标仓库 {repo} 不存在」
     - 配置了但 push 失败 → 「发布失败：{具体原因}」
     - 不得笼统报「未配置」
9. 多平台发布：一个平台失败不中断其他平台——最终汇总摘要
10. 回复（评论模板）：

⚠️ **评论里的 ClawHub URL 必须用 ownerHandle 拼接（`https://clawhub.ai/{ownerHandle}/skills/{slug}`）。ownerHandle 获取优先级：发布时使用的 > GET /api/v1/skills/{slug} 返回的 owner.handle > GET /api/v1/whoami 返回的 user.handle。不得用 API URL（`/api/v1/skills/{slug}`）兜底，不得漏 `/skills/` 段，不得自己拼 URL。**

⚠️ **发布完成后必须发 Issue 评论告知，跳过此步 = 事件未完成处理：**

```bash
curl -s --max-time 30 -X POST \
  -H "PRIVATE-TOKEN: $BOT_G...OKEN" \
  -H "Content-Type: application/json" \
  -d '{"body": "[SRE Agent] ✅ Skill 发布完成\n\n## 发布结果：{name}\n\n| 平台 | 状态 | 版本 | URL | 安装命令 |\n|------|------|------|-----|----------|\n| ClawHub | ✅ 已提交（安全扫描中）/ 🔴 失败 | v{clawhub_version} | https://clawhub.ai/{ownerHandle}/skills/{slug} | `clawhub install {slug}` |\n| GitHub | ✅ 成功/⚠️ 跳过 | v{github_version} | {url} | — |\n| GitCode | ✅ 成功/⚠️ 跳过 | v{gitcode_version} | {url} | — |\n\n**文件指纹**：{SHA-256 前 8 位}\n**发布时间**：{UTC ISO}\n\n**版本号说明**：ClawHub 可能因版本占用自动 bump，版本号与其他平台不同。跳过的平台版本填「—」。"}' \
  "https://api.gitcode.com/api/v5/repos/$REPO_FULL_NAME/issues/$ISSUE_NUMBER/comments"
```

评论 body 格式：
```
[SRE Agent] ✅ Skill 发布完成

## 发布结果：{name}

| 平台 | 状态 | 版本 | URL | 安装命令 |
|------|------|------|-----|----------|
| ClawHub | ✅ 已提交（安全扫描中）/ 🔴 失败 | v{clawhub_version} | https://clawhub.ai/{ownerHandle}/skills/{slug} | `clawhub install {slug}` |
| GitHub | ✅ 成功/⚠️ 跳过 | v{github_version} | {url} | — |
| GitCode | ✅ 成功/⚠️ 跳过 | v{gitcode_version} | {url} | — |

**文件指纹**：{SHA-256 前 8 位}
**发布时间**：{UTC ISO}

**版本号说明**：ClawHub 可能因版本占用自动 bump，版本号与其他平台不同。跳过的平台版本填「—」。

（失败平台示例）
| ClawHub | 🔴 失败 | — | — | 令牌无效，请到设置页更新 |
```

## 下架流程
1. 读取各平台令牌（优先环境变量，缺失时回退 .project-info/ 下 repo-credentials.json）；令牌缺失/401/403 → 跳过该平台并输出提示
2. 按平台下架:
   - ClawHub: DELETE /api/v1/skills/{slug}（组织配置时带 ?ownerHandle=）——软删除 30 天；**扫描中不可删除：遇到该状态直接失败不重试**
   - GitHub/GitCode: 调脚本封装 clone→备份→删除→1次commit→push：
     ```bash
     bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh \
       --action unpublish --platform github \
       --skill-name {name} --issue-num {issue_number}
     bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh \
       --action unpublish --platform gitcode \
       --skill-name {name} --issue-num {issue_number}
     ```
     目录不存在时脚本输出 PUSHED=0 + MESSAGE=无需下架（非失败）；脚本内置 backup/pre-delist-{timestamp} 备份分支
3. 执行后校验平台状态，汇总回复（同发布结果表格格式）

## ClawHub payload 契约（已验证，协议存档）
- POST /api/v1/skills（multipart: payload JSON + files[]）
- payload 字段: slug(必填) / displayName(必填) / version(必填) / changelog(必填) / tags(数组) / acceptLicenseTerms(true) / ownerHandle(可选)
- **changelog 生成方式**：从 SKILL.md description 字段提取，或从 git log 最近变更生成（如 "v0.2.0: 修复 base_url 交互输入 + 新增文件上传支持"）。不得填空字符串——ClawHub 会返回 400
- files[]: 每个文件带 contentType（SKILL.md + 支持文件——整个 skill 目录）
- 发布提交成功 = ok（平台异步安全扫描——通过后公开——被拒可修正重发）
- ⚠️ **评论里 ClawHub 状态应报「✅ 已提交（安全扫描中，通过后公开）」而不是「✅ 成功」——扫描中用户还不能安装**
- 撤销: DELETE /api/v1/skills/{slug}（软删除 30 天——扫描中不可删——扫描完成可删）

## 判断标准

- ClawHub：POST 返回 200 = 已提交（安全扫描中，通过后公开）；GET 确认 status=published = 发布成功
- GitHub/GitCode：脚本输出 PUSHED=1 = push 成功；SHA-256 校验一致 = 内容正确
- 跳过的平台：版本填「—」，状态填「⚠️ 跳过」+ 真实原因
- 失败的平台：版本填「—」，状态填「🔴 失败」+ 原因

## 分析结论示例

```
分析结论：
- 场景：skill 发布（A2A 触发，tester 测试通过后交棒 sre）
- Skill：blog-manager v0.1.0
- 触发方式：A2A 自动 → 直接发布
- ClawHub：v0.1.0 被占用 → bump v0.2.0 → 已提交（安全扫描中）
- GitCode：push 到 signway/grape_skill → SHA-256 校验一致
- GitHub：GITHUB_TOKEN 无写入权限 → 跳过
- 必须执行：Issue 评论汇总 → 任务结束
```

## 参考
- 调用 GitCode API 时，**必须以 `references/gitcode-api-reference.md` 为准**——严禁自行拼写 API 路径

## 任务结束

完成所有业务动作（发布 / 下架 / 回复）后，在最终答复末尾输出以下 JSON 块，然后**结束任务**：

```json
{
  "actions": ["做了什么，如 published:skill-xxx:clawhub / published:skill-xxx:github / skipped:gitcode"],
  "conclusion": "处理结论（发布成功/部分失败/全部失败）",
  "artifacts": ["产生的产物：ClawHub URL / GitHub URL / GitCode URL / 评论"],
  "next_step": "下一步建议；如需重试失败平台则说明",
  "issue_summary": "Issue 最新聚合结论（紧凑文本，用 | 分隔）"
}
```

注意：
- 只输出一个 JSON 块，放在答复末尾
- **不得用长流程叙述替代 JSON 块**
- 若无法确定动作或结论，相应字段填空数组/空字符串，不要伪造
