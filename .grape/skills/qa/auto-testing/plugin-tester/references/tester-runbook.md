# plugin-tester 执行走查表（runbook）

> 环境变量：`$REPO_OWNER`/`$REPO_NAME`/`$ISSUE_NUMBER`/`$REPO_FULL_NAME`/`$GRAPE_WORK_BRANCH`。owner 只用 `$REPO_OWNER`。
> 只读不写：不改被测插件包；产物只有报告与 A2A。
> 防抖纪律：一条命令取到即继续；命令失败先看完整错误；不重复相同命令试错。

---

## Step 0 · 前置（必须最先）

```bash
grape --version                       # >= 0.3.2，否则按 grape-cli-installation.md 自装
grape refresh
echo "token=${GRAPE_SESSION_TOKEN:+ok} owner=$REPO_OWNER repo=$REPO_NAME issue=$ISSUE_NUMBER pr=${PR_NUMBER:-none}"
grape tasks current --json-out
grape git config user.name  "$BOT_USERNAME"
grape git config user.email "$BOT_USERNAME@noreply.git.com"
```

## Step 1 · 取被测插件包

A2A payload 携带 `pr_number` → 拉取 PR 变更清单，定位插件包目录（plugin.json 所在）：

```bash
grape git-repo pulls files --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
grape git-repo pulls get  --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$PR_NUMBER" --json-out
```

PR 未就绪/无插件包 → 评论说明阻塞，结束（不空测）。

## Step 2 · L0 静态校验（一票否决）

```bash
python3 -c "import json;json.load(open('<pkg>/plugin.json'))"   # 可解析性
```

按 `references/tester-checklist.md` §1 核对：必填字段（name/version/adapters/commands/compat）、目录结构、SKILL.md frontmatter 与 manifest 一致。**任一失败 → 直接 🔴**，跳过后续层。

## Step 3 · 契约校验

按 `references/tester-checklist.md` §2 逐项：command↔adapter 引用一致、method/path 合法、credentials 无明文且 scope 覆盖、permissions 域覆盖 base_url、compat.engines 形态口径、timeout 必填。逐项记录 ✅/❌。

## Step 4 · 免凭据自检（协议层验证）

```bash
# 方法名探测：只读端点存在性（404/405 = 路由层反馈正常）
curl -s -o /dev/null -w "%{http_code}" --max-time 30 "https://api.example.com/v1/buckets"
# 401 分层：无凭据应得 401/403（证明鉴权注入位正确），5xx = 注入位声明可疑
curl -s -o /dev/null -w "%{http_code}" --max-time 30 "https://api.example.com/v1/whoami"
```

- cli 型：`timeout 30 <binary> --help` 验证 binary 可用且白名单外命令不可达。
- 结果记入矩阵；网络不可达（目标产品不在白名单/内网）→ 该用例标「环境受限」降级，不判红。

## Step 5 · 引擎路由实测（compat 抽样）

凭据可用时：按 compat.engines 抽样双引擎各跑一条 `level=read` 命令（经 Runner 语义：env 注入、超时、审计字段齐全）。
凭据缺失/过期 → 转 Step 6，不判红。

## Step 6 · 凭据缺失/过期引导

- 报告标注「待配置凭据」+ 引导（场景高级选项按 credential spec 配置 / session 类重新授权）。
- 需凭据用例全部无法执行 → 结论「待部署后冒烟」降级，不虚构通过。

## Step 7 · 报告 + 分流（必做）

```bash
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$ISSUE_NUMBER" \
  --json '{"body": "[QA Agent] ✅ 测试通过\n\n{四层矩阵}\n\n分流：A2A sre plugin-publish 上架。"}'
grape git-repo pulls comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" \
  --number "$PR_NUMBER" \
  --json '{"body": "[QA Agent] ✅ 测试通过\n\n{四层矩阵摘要}"}'
```

失败模板：`[QA Agent] 🔴 测试失败` + 失败项（规则编号 + 原因 + 修复建议），A2A 打回 coding（每项至多 1 次）。

```bash
grape scenes detail --scene_id "$GRAPE_SCENE_ID" --json-out   # 确认下游节点存在
grape a2a handoff --json '{"agent": "sre", "payload": {"repository": "'"$REPO_FULL_NAME"'", "account": "'"$ACCOUNT"'", "issue_number": '"$ISSUE_NUMBER"', "pr_number": '"${PR_NUMBER:-0}"', "context": "插件 {name} 测试通过（四层矩阵），请上架发布。"}}' --json-out
grape a2a handoff --json '{"agent": "coding", "payload": {"repository": "'"$REPO_FULL_NAME"'", "account": "'"$ACCOUNT"'", "issue_number": '"$ISSUE_NUMBER"', "pr_number": '"${PR_NUMBER:-0}"', "context": "插件 {name} 测试失败：{原因摘要}。请修复后重新提交流程。"}}' --json-out
```

## Step 8 · 任务完成流程（必做）

按 SKILL「任务完成流程」调用 `grape tasks complete`（next_step=`a2a_sre` 或 `a2a_coding`）。`already:true` 忽略，结束任务。

---

## 错误排查速查

| 现象 | 处理 |
|------|------|
| `No such command 'X'` | 核对 grape-cli-reference.md；仍不确定 `grape commands <关键词>` |
| plugin.json 解析失败 | L0 直接 🔴，报告给 coding |
| 探测全 5xx | 目标产品异常；「环境受限」降级记录，不判红 |
| 401/403 | 平台凭据问题：说明并停止，不换 token |

## 明确禁止

- ❌ 跳过 L0 一票否决直接动态测试。
- ❌ 修改被测插件包 / 替 coding 修复。
- ❌ 凭据明文进报告/评论/日志。
- ❌ 无凭据时虚构「实测通过」。
- ❌ 裸 `git`（一律 `grape git`）。
- ❌ 不因"等待"跳过收尾（tasks complete）。
