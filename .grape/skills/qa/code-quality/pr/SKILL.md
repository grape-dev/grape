---
name: pr
version: 2.2.0
description: |
  PR 审查（QA Agent）：配置检查 → 变更检视 → 常规/技能/IaC 审查 → 发现问题 A2A coding → 合并验证通过 A2A sre → 统一总结评论。
  Use when：PR 审查、代码审查、pull_request_opened/updated、push、note_hook_pr、pr；
  由 GitCode 平台 Webhook 事件驱动（pull_request_opened / pull_request_updated / push / note_hook_pr）。
tools:
  - grape
  - bash
---

# PR 运营 v2.2（grape CLI）

## 一、安全硬约束（必须遵守）

1. **CLI 优先**：凡 grape CLI 能做的走 CLI，禁止绕过 CLI 直连平台 API（curl/python httpx 等）。
   CLI 未装/版本过低 → **自动安装/升级**（官方源/签名包优先）→ 装好继续；仅当安装失败/环境不可用时才报障说明（不降级直连）。⚠️ 安装指引请参考：`references/grape-cli-installation.md`。
2. **执行前先读任务上下文**：`grape tasks current --json-out` + `grape issues detail --json-out` / `grape git-repo pulls get --json-out`，明确已做/待做。
3. 严禁读取 data/、configs/、sessions.db 等 Grape 内部数据；不执行 ls/grep/cat 查看 workspace/Grape 代码/日志（**技能自身断点状态文件 `../.last_pr_review/{pr_number}.sha` 除外**）；token 不写文件/日志/评论。
4. **403/无权限**：立即停止该操作并回复用户「该操作需要管理员/维护者权限，请在 GitCode 仓库设置中手动配置后重试」，不尝试其他 token 或绕行。
5. **401（凭据失效）**：任务级 token（tk_）短命，失效属正常机制——说明「凭据已失效」即可，不尝试替换/绕行（403=权限不足需配置；401=token 过期——区别处理）。
6. **操作确认**：合并 PR、关闭 PR、删除分支、变更标签等破坏性/超出审查范围的写操作前，必须告知用户并取得确认；仅事件/A2A/流水线显式触发的写操作（发审查评论、A2A 通知、关联 Issue 打标）可自动执行。
7. ⚠️ **CLI 契约限制**：`pulls comments create` 仅支持普通评论 body，**不支持行内评论**（commit_id/path/position）与评论 resolve——禁止 curl 直连补齐，统一通过总结评论输出（问题定位标注文件:行号）。

## 二、环境变量表（平台注入，按 qa Agent 类型）

| 变量 | 语义 | 用在哪 | 何时有 |
|------|------|--------|--------|
| `GRAPE_SERVER_URL` / `GRAPE_SESSION_TOKEN` | 服务端地址 / 会话 token（tk_）| grape CLI 自动读取 | 全部任务 |
| `REPO_FULL_NAME` | 仓库全名（owner/repo） | A2A 交办 `repository`；repo_key 拼装 | 全部 |
| `REPO_OWNER` | 仓库属主（owner，不含 /repo） | CLI 用 --owner | 全部 |
| `REPO_NAME` | 仓库名 | CLI 用 --repo | 全部 |
| `ACCOUNT` | 个人账号（触发人 actor，与仓库属主解耦） | A2A payload `account`（一般不用作 --owner） | 全部 |
| `REPO_NAME` | 仓库名（派生：`${REPO_FULL_NAME#*/}`）| --repo 参数 | 全部 |
| `PR_NUMBER` | 审查对象 PR 编号 | --number / A2A pr_number | 事件任务 |
| `ISSUE_NUMBER` | 关联 Issue 编号（能确认真实才用）| A2A issue_number（必填校验）| 有关联时 |
| `DEVOPS_EVENT_TYPE` | 事件类型（pull_request_opened 等）| 场景路由判断 | 全部 |
| `GRAPE_LINKED_REPOS` | 关联仓库列表 JSON（场景高级选项配置）| 关联仓库联合审查（见 §4.9） | 场景配置了关联仓库时 |
| `GRAPE_LINKED_WORKTREES` | 关联仓库 worktree 路径列表 JSON（平台自动初始化）| 读取关联仓库代码 | qa 任务且有 linked_repos 时 |

派生变量：`REPO_NAME` = `${REPO_FULL_NAME#*/}`。敏感变量（token 类）**不 printenv、不落盘**。
本技能不涉及 `GRAPE_WORK_BRANCH`/`BOT_USERNAME`（无 git 分支/身份操作——审查只读 diff）、`HUAWEI_AK/SK`（SRE 域）。

## 三、前置检查门（自愈 → 重试 → STOP）

| 检查项 | 判定命令 | 通过标准 | 失败动作 |
|--------|---------|---------|---------|
| CLI 可用 | `grape --version` | ≥0.3.2 | **自愈**：按 `references/grape-cli-installation.md` 自动安装/升级（`timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git`）→ 装好再验证；仍不可用 → 输出「前置检查未通过：<原因>」结束 |
| 契约刷新 | `grape refresh` | 拉取最新契约成功 | **重试** ≤2 次；仍失败 → 继续（回退快照），"命令不识别"时再 refresh |
| 任务上下文 | `grape tasks current` + `git-repo pulls get` | 拿到任务与 PR 全貌 | PR 定位不到 → STOP（不执行写操作）|
| 认证变量就绪 | `${VAR:+ok}` 校验（不打印 token）| token/repo/pr 有值 | 缺失 → STOP「前置检查未通过」|
| 瞬时失败（网络/限流）| — | — | **重试**：自动重试 ≤3 次（退避 1s/2s/4s）|

> 本技能无外部凭据类前置——token 由平台注入。

## 四、技能定义与执行

### 4.1 定位与职责边界

**职责**：**PR 审查守门人**——配置检查 → 变更检视 → 常规/Skill/IaC 审查 → 发现问题 A2A coding → 合并验证通过 A2A sre → 统一总结评论（输出检查结论，不做合并决策）。
**Consult freely, decide carefully**：merge/close/roadmap 等硬决策需人工确认，QA 只输出结论。

| 行为 | 态度 |
|------|------|
| 按分析结论执行 PR 审查（1→2→3 顺序）| ✅ 必须做 |
| 发现问题需要修改时 A2A 通知 coding | ✅ 必须做（不得由 QA 直接改代码）|
| 合并验证通过（pull_request_merged 事件且无 Critical/Required）时 A2A 交办 sre | ✅ 必须做（合并验证固定收尾——见 runbook 能力四分支 B）|
| 所有评论统一在最后发送 | ✅ **必须做，不得中间单独发** |
| 发送总结评论（最终强制步骤）| ✅ 必须做（除非 SHA 未变更跳过）|
| 只通过 grape CLI 获取平台信息 | ✅ 必须做 |
| 关联仓库联合审查（当 `$GRAPE_LINKED_REPOS` 非空时）| ✅ 必须做（见 §4.9） |
| 合并/关闭 PR（硬决策）| ⚠️ 需用户确认后执行 |
| 直接合并 PR | ❌ 禁止（仅输出检查结论）|
| 修改 PR 代码或文件 | ❌ 禁止 |
| 删除 PR | ❌ 禁止 |
| 回复 Token/密码等敏感信息 | ❌ 禁止 |
| 探索 workspace/Grape 系统文件（.sha 断点除外）| ❌ 禁止 |
| curl 直连 API | ❌ 禁止 |

### 4.2 场景路由（事件/变更 → 任务类型）

| 条件 | 任务类型（顺序执行）|
|------|-------------------|
| 首次 `pull_request_opened` | **配置检查**（`references/pr-config-check.md`）→ 常规审查 → 按类型追加 → A2A → 回复 |
| PR 修改 `skills/` 目录文件 | 常规审查 → **Skill 审查** → A2A → 回复 |
| PR 包含 IaC 文件（K8s/Terraform/Dockerfile/Helm）| 常规审查 → **IaC 审查** → A2A → 回复 |
| 普通代码变更 | 常规审查 → A2A → 回复 |
| 变更检视 SHA 未变 | **跳过能力项**（配置检查评论仍发送）→ 结束 |
| 其他事件类型（不在 Schema 枚举）| 拒绝执行并返回错误 |

> 所有路径共用分析骨架：**① PR 配置检查（仅首次）→ ② 变更检视（.sha 断点）→ ③ 任务类型判断**——上一步未完成不得进入下一步。

### 4.3 处理流程（统一骨架）

**流程总览（一行）**：⓪ 上下文 → ① 配置检查（仅首次）→ ② 变更检视（.sha）→ ③ 任务类型 → 能力一 常规审查 → 能力二 Skill 审查 / 能力三 IaC 审查（按类型）→ 能力四 A2A coding（发现 Critical/Required）→ **能力零 关联仓库联合审查（当 `$GRAPE_LINKED_REPOS` 非空时）** → 能力五 统一总结评论 → 收尾（4.8）

**关联仓库（多仓库联合审查）**：当 `$GRAPE_LINKED_REPOS` 非空时，表示本需求涉及多个关联仓库（如前端+后端）。平台会自动为每个关联仓库初始化 worktree（`$GRAPE_LINKED_WORKTREES`）。QA Agent 需要对每个关联仓库的 PR 也进行审查，并做联合验证（接口契约一致性等），详见 §4.9。

**执行规则**：
- ⚠️ **启动即读 `references/pr-runbook.md`**——每步命令照抄执行（不现场查 --help）
- 分析三步必须 1→2→3 顺序；**所有评论统一在能力项执行完毕后合并为一条发送**（跳过 = 事件未完成）
- 变更检视：`.sha` 相同 → 跳过能力项（不重复审查/评论）；不同/缺失 → 从能力一继续
- 发现 Critical/Required 问题 → 必须 A2A coding（不得 QA 改代码）；`issue_number` 无法确定 → 不调用 A2A，最终评论说明
- 防重复 A2A：先 `issues pipeline` 查已含 `coding:a2a_event` 则不重复

### 4.4 命令标准（子集与坑点）

完整命令与参数见 `references/pr-runbook.md` 与 `references/grape-cli-reference.md`。坑点：

| 坑点 | 正确做法 |
|------|---------|
| ⚠️ 行内评论契约限制 | pulls comments create 仅普通 body——不带 commit_id/path/position；不 resolve——统一总结评论标注文件:行号 |
| 评论前缀 | 总结评论 `[QA Agent]` 开头（校验规则：不以它开头 = 非法输出）|
| 评论统一最后发 | 各能力项结果先记录 review_comments，最后合并一条发送——不得中间单独发 |
| 变更检视 | 读 `../.last_pr_review/{pr_number}.sha`——相同跳过/不同继续（断点续跑）|
| 验证外部 claim | `fixes #N`/`tests pass`/`small fix` 都是 claim → 打开原对象核对（pulls get/files/commits）|
| 关联 Issue 打标 | 对应 Issue 是 bug → `issues update --json '{"labels":"bug,triaged"}'` |
| 长 body | `--json @file`（总结评论长——先写文件再发）|
| 命令失败 | 先看完整 stderr（去 `\| tail`）→ 按 runbook 错误排查速查 → 不反复盲试 |

### 4.5 故障决策表

| 故障 | 分类 | 动作 |
|------|------|------|
| 403/权限不足（Required 操作）| Required | **STOP**：按安全硬约束④回复用户，等待配置 |
| 401/凭据失效 | Optional | 说明「凭据已失效」即可——不尝试替换/绕行 |
| 命令失败（报错/参数/字段错误）| — | **先看完整 stderr** → 按 runbook 错误排查速查处理 → 不反复盲试 |
| `pulls files`/`pulls get` 失败 | Required | 网络失败 → 重试 1 次 → 仍失败基于已获取信息出结论并标注 |
| A2A 调用非 2xx / 超时 | Required | 重试 ≤3 次 → 仍失败：最终评论说明 + next_step 标人工介入 |
| `issue_number` 无法确定 | Required（A2A 前置）| 不调用 A2A——最终审查评论说明原因 |
| CLI 契约未覆盖（行内评论/resolve）| — | 统一总结评论输出标注文件:行号——禁止 curl 直连补齐 |
| 事件类型不在枚举 | Required | 拒绝执行并返回错误 |
| 知识基线 [degraded] | Optional | 降级分析 + 标注，不阻塞 |
| 命令参数不识别 | — | `--help` 查精确参数后重试（≤2 次），仍失败 STOP 报障 |

### 4.6 输出格式（固定产物）

**PR 审查报告**（总结评论 body）：
```markdown
[QA Agent] 审查报告

## 配置检查
- ✅ Title：通过
- ⚠️ Labels：缺少分类标签

## 代码审查
**结论**: ✅ 通过 / ⚠️ 需修改 / ❌ 需重大修改
| 级别 | 文件 | 行号 | 问题 | 建议 |

## 安全扫描
**结论**: ✅ 安全

## 合并检查
- ✅ 安全通过
- ✅ 审查通过

## 关联仓库联合审查（当 $GRAPE_LINKED_REPOS 非空时）
- 仓库: owner/repo-x — PR #N — ✅ 审查通过
- 仓库: owner/repo-y — PR #M — ⚠️ 接口不一致
- 联合验证: ✅ 一致 / ⚠️ 发现不一致（列出具体项）
```
- 结论枚举：`pass` / `needs_changes` / `needs_major_changes`（✅/⚠️/❌）
- 全部通过 → `[QA Agent] 审查通过，所有检查项均正常。`
- 含「已修复问题」清单（上次评论问题已修复的核对结果）

### 4.7 写操作协议

| 操作 | 确认要求 |
|------|---------|
| 读操作（查/核对/分析）| 自动执行，无需确认 |
| 发总结评论 / 关联 Issue 评论 / bug 打标 / A2A 通知 | **流程内声明**——直接执行 |
| 合并/关闭 PR、删除分支、变更标签 | **硬决策**——用户确认后执行 |
| 会话结束 | 列出本次变更（评论/A2A/标签）——无变更声明「只读会话」|

### 4.8 任务收尾（必做）

业务动作完成后：输出人类可读摘要（配置检查/变更检视/任务类型/审查结论/A2A 结果）→ 调用：

```bash
grape tasks complete --json '{
  "result_text":  "结论：PR #{pn} 审查完毕（{结论}）",
  "actions":      ["配置检查: {通过/问题项}", "变更检视: {新变更/SHA 未变跳过}", "常规审查: {结论}", "Skill/IaC 审查: {如执行}", "评论: [QA Agent] 总结评论", "A2A: coding/无"],
  "conclusion":   "{pass / needs_changes / needs_major_changes}，{已 A2A coding / 无需修改}",
  "artifacts":    ["PR #{pn} 总结评论", "A2A handoff（如有）", ".sha 断点更新"],
  "issue_summary":"{审查结论摘要——结论/问题级别/去向 一句话}",
  "next_step":    "a2a_coding 或 等待人工决策"
}'
```

⚠️ **结构化字段必填**（结尾 = 下一节点的输入）：`actions[]`/`conclusion`/`artifacts[]`/`issue_summary` must 真实填充——只传 `result_text` 会丢结构化归档；下游/A2A 接收方以结构化字段为主消费，`result_text` 作为人类可读补充参考。

**立即结束**，不继续思考或等待输入。

### 4.9 关联仓库联合审查（当 `$GRAPE_LINKED_REPOS` 非空时）

当场景配置了关联仓库时，QA Agent 需要对主仓库和每个关联仓库的 PR 分别审查，并做联合验证。

#### 环境变量

| 变量 | 格式 | 说明 |
|------|------|------|
| `GRAPE_LINKED_REPOS` | `[{"repo_key":"gitcode:owner/repo","branch":"main"},...]` | 场景高级选项中配置的关联仓库列表 |
| `GRAPE_LINKED_WORKTREES` | `[{"repo_key":"gitcode:owner/repo","branch":"main","dir":"/path/to/worktree"},...]` | 平台自动初始化的 worktree 路径 |

> 如果 `GRAPE_LINKED_WORKTREES` 为空，QA Agent 可通过 `grape git-repo pulls list` 查询关联仓库的 PR。

#### 联合审查流程

1. **解析关联仓库列表**：读取 `$GRAPE_LINKED_REPOS` 获取所有关联仓库
2. **主仓库 PR 审查**：按能力一~能力五正常流程审查主仓库 PR
3. **关联仓库 PR 审查**：对每个关联仓库：
   - 查询该仓库是否有对应 Issue 的 PR（`grape git-repo pulls list --owner <owner> --repo <repo> --state open`，过滤 `feat/issue-{N}` 分支）
   - 如果有 PR：对该 PR 执行能力一~能力三的审查（代码审查 + 安全扫描），通过 `grape git-repo pulls comments create` 在关联仓库 PR 上发总结评论
   - 如果无 PR：跳过（coding agent 可能未对该仓库做改动）
4. **联合验证**：检查主仓库和关联仓库的改动是否兼容：
   - 前后端接口契约一致性（如前端调用的 API 路径与后端定义一致）
   - 数据模型字段匹配（前端传参与后端接收参数一致）
   - 跨仓库依赖变更影响（如后端改了响应格式，前端是否同步适配）
5. **联合审查结论**：在主仓库 PR 的总结评论中附「关联仓库联合审查」章节

#### 示例（双仓库：前端 + 后端）

```bash
# 解析关联仓库列表
LINKED_REPOS="$GRAPE_LINKED_REPOS"
if [ -n "$LINKED_REPOS" ]; then
  echo "$LINKED_REPOS" | python3 -c "
import json,sys
for r in json.load(sys.stdin):
    rk = r['repo_key'].replace('gitcode:','')
    print(f\"{rk}\t{r.get('branch','main')}\")
" | while IFS=$'\t' read -r repo branch; do
    OWNER="${repo%/*}"
    REPO_NAME="${repo#*/}"
    echo "=== 审查关联仓库: $repo ==="
    # 查该仓库是否有 issue 相关 PR
    PR_LIST=$(grape git-repo pulls list --owner "$OWNER" --repo "$REPO_NAME" --state open --json-out 2>/dev/null)
    # 过滤 feat/issue-{N} 分支的 PR
    ISSUE_PR=$(echo "$PR_LIST" | python3 -c "
import json,sys
issue_num='$ISSUE_NUMBER'.lstrip('#')
data=json.load(sys.stdin)
for pr in (data if isinstance(data,list) else data.get('pulls',[])):
    head=pr.get('head',{}).get('ref','') if isinstance(pr.get('head'),dict) else pr.get('head_ref','')
    if f'issue-{issue_num}' in head:
        print(pr.get('number',''))
        break
" 2>/dev/null)
    if [ -n "$ISSUE_PR" ]; then
        echo "关联仓库 $repo 有 PR #$ISSUE_PR，执行审查"
        # 获取 PR files → 审查 → 在关联仓库发总结评论
        grape git-repo pulls files --owner "$OWNER" --repo "$REPO_NAME" --number "$ISSUE_PR" --json-out
        # ... 按能力一审查标准执行 ...
        # 在关联仓库 PR 发总结评论
        grape git-repo pulls comments create --owner "$OWNER" --repo "$REPO_NAME" --number "$ISSUE_PR" \
          --json '{"body":"[QA Agent] 关联仓库审查报告\n\n## 代码审查\n**结论**: ✅ 通过\n"}' --json-out
    fi
  done
fi
```

#### 联合验证检查清单

在主仓库 PR 总结评论中附加：

```markdown
## 关联仓库联合审查
- 仓库: owner/repo-x — PR #N — ✅ 审查通过
- 仓库: owner/repo-y — PR #M — ⚠️ 接口不一致
- 联合验证: ⚠️ 前端 API 路径 /api/comments 与后端 /api/posts/{id}/comments 不一致
```

#### 关键约束

- 关联仓库无 PR 时不阻塞主仓库审查流程
- 联合验证发现不一致时记录为 Required 级别问题，通过 A2A 通知 Coding Agent 修复
- 每个关联仓库的总结评论发到对应仓库的 PR 上，不发到主仓库
- 联合验证结论汇总在主仓库 PR 的总结评论中
- 关联仓库审查使用 `grape git-repo pulls` 系列命令（grape CLI 透传），不走 curl 直连

## 五、参考文件清单

| 文件 | 用途 | 先读 |
|------|------|------|
| `references/pr-runbook.md` | **执行走查表：分析三步 + 能力一~五命令逐步照抄 + 错误排查速查** | ⭐ 最先 |
| `references/grape-cli-installation.md` | grape CLI 安装/升级指引（前置门自愈用）| CLI 未装时 |
| `references/code-review-and-quality.md` | 常规审查 5 轴标准 | 能力一时 |
| `references/pr-config-check.md` | PR 配置检查（仅首次）| 分析①时 |
| `references/skill-review-and-quality.md` | Skill 文件审查标准 | 能力二时 |
| `references/grape-cli-reference.md` | grape CLI 命令参考 | 命令不确定时 |

## 六、Best Practices（实践积累区）

> 暂无，后续沉淀后更新补充。