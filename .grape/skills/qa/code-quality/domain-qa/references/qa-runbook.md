# QA 执行走查表（runbook）

> 目的：把 domain-qa 每一步**要做什么 + 用哪条确切命令**固化下来，Agent **照着执行、不要查 `--help` / `grape commands`**。
> 环境变量直接使用：`$REPO_OWNER`（owner）、`$REPO_NAME`（repo 名 = `${REPO_FULL_NAME#*/}`）、`$ACCOUNT`（触发人 actor）、`$ISSUE_NUMBER`、`$DEVOPS_EVENT_TYPE`、`$GRAPE_SESSION_TOKEN`/`$GRAPE_SERVER_URL`（grape 自动读取——**不传 `--token`、不 export 其他 token**）。
> QA 硬约束——**不改仓库、不装依赖、不起后台**：只读平台交互 + 只复制自检/冒烟脚本进 `scripts/` 后执行，其余命令一律只读。
> 判定红线/具体判定细则见 `references/qa-checklist.md`；**平台交互一律 grape CLI**——禁止 curl 直连平台 API。

## Step 0 就绪检查（前置门——按序执行）
```bash
grape refresh 2>&1 && echo REFRESH_OK          # 契约保鲜；失败重试 ≤2 次后回退内置快照继续
grape tasks current --json-out                 # 当前任务（事件/上下文/基线提示）
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out 2>&1 | head -80
# ISSUE 缺失 → grape git-repo issues get / issues list 定位；定位不到 → STOP（不执行写操作）
# Q2-0 环境就绪判定（先读，不具备 → conclusion 未就绪，仍给出非阻塞问题建议）
```

## 场景 A：接管 A2A 验收（Q1 → Q2 → Q3）

```bash
# Q1 读取 Issue 全貌 + 评论（找 Baseline 回复链）
grape issues detail --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
grape git-repo issues comments list --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" --json-out
#   三条红线（先行判定，任一不满足 → 不向下执行）：
#   a) body 含 spec-in-progress 完整四段   b) 含改动汇总路径（scripts 复制列表 或 Band 说明）
#   c) 只读预算充足（改动文件 ≤40、测试脚本 ≤2、根目录余量 >2GiB）

# Q2 四条验收（自检脚本优先，禁手写）——先把脚本从远程摘到 scripts/ 再执行：
#   差异兜底（仅当 scripts3A 缺失）：git diff --name-only 若 ≤40 用 `git diff --stat $(REPO base)`
#   ⚠️ 以下每一条 | tee scripts/log-*.log  保留原始输出证据
source scripts/3a_ext.sh 2>/dev/null          # 存在才 source（平台已注入则直接使用）
scripts/validate_domain_project.py "$REPO_NAME" --domain "$DOMAIN_NAME" --repo-owner "$REPO_OWNER" | tee scripts/log-1-validate.log
scripts/ws_smoke.py "$REPO_NAME" | tee scripts/log-2-ws.log
timeout 300 npm run build 2>&1 | tee scripts/log-3-build.log   # ∵ ensure 未附着左右 run 的语境必须在工作区执行
python scripts/transform_check.py --changes-file scripts/changes.json --host "$HOST_DISPLAY_ADDRESS" --port "$SMOKE_EXPOSE_PORT" | tee scripts/log-4-transform.log
#   4 小项手检收口：后端命令补全/开发模式分别查、prompt 原型逐字核对、产物镜像自查，结论载入 changes.json

# Q3 判定 + 交办（hosted/IDEA 环境差异按 SKILL 第五条执行；无规则命中 → QA-only 路线）
#   未通过（Q2 任一 failed）→ 回退 domain-coding 重做，评论指出具体违规项：
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body":"[Domain QA Agent] > {Q2 验收逐项结果；违规项与证据路径；修改要求}"}'
grape a2a handoff --json '{"agent":"coding","payload":{"repository":"$REPO_FULL_NAME","account":"$ACCOUNT","issue_number":'$ISSUE_NUMBER',"next_hop":"qa","allowed_agents":["issue","coding","qa","sre"],"context":"QA 验收未通过，请按评论违规项修复后重新交办 domain-qa"}}'
#   通过（Q2 全 passed）→ 评审判定通过，评论「QA 验收通过」（含逐项证据）
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body":"[Domain QA Agent] > QA 验收通过：{逐项 passed 证据摘要}"}'
```

## 场景 B：修改请求复检（利用已下载脚本/日志目录，节省资源）

```bash
# B1 定位该 Issue 本工作区确认的证据目录/脚本：
#   scripts/log-*.log  或  scripts/kept-*（同 `$REPO_NAME` + 同变更集），存在则复用
# B2 增量复检：仅对「改动文件 + Spec body」作 diff 对比复检 ① 与 ③（∅ 改动则跳过）
source scripts/3a_ext.sh 2>/dev/null           # 存在才 source（平台已注入则直接使用）
scripts/validate_domain_project.py "$REPO_NAME" --domain "$DOMAIN_NAME" --repo-owner "$REPO_OWNER" | tee scripts/log-1-validate.log
# B3 判定：增量复检通过 → 沿用原结论 + 评论「复检通过（含增量证据）」；
#        未通过 → 评论指出增量违规项 + A2A 回退 domain-coding（照场景 A 未通过分支）
grape git-repo issues comments create --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER" \
  --json '{"body":"[Domain QA Agent] > {复检结论与证据}"}'
```

## 收尾（结构化——must 真实填充）
```bash
grape tasks complete --json '{
  "result_text":  "QA 处理完毕（结论：{通过/未通过/未就绪 + 逐项证据摘要}）",
  "actions":      ["环境就绪: {是/否+原因}", "读 Issue+评论: {#n}", "验收: validate {pass/fail} / ws_smoke {pass/fail} / build {pass/fail} / transform {pass/fail}", "判定: {passed/回退 coding/未就绪}"],
  "conclusion":   "{QA 验收通过 / QA 验收未通过→回退 coding / 环境未就绪→建议}",
  "artifacts":    ["scripts/log-*.log ×{n}", "评论: {评论位置}", "A2A handoff: {agent=coding, status=ok/无}"],
  "issue_summary":"{Q2 验收结论 一句话}",
  "next_step":    "终止或 等待 coding 修复回访"
}'
# 结束任务——不继续思考/不追加内容
```

## 错误排查速查（先看完整 stderr，不截断不盲试）

| 现象 | 处理 |
|------|------|
| `No such command` | 命令名抄错 → 核对本 runbook / `references/grape-cli-reference.md`；仍不确定 `grape commands <关键词>` |
| `unknown method` / 命令不存在但应为平台功能 | 契约过期 → `grape refresh` 后重试 |
| `run_all_request` 含不识别字段 | `--json` 有契约不允许字段 → `grape <group> <verb> --help` 看 Body fields |
| 401 | token 失效（tk_ 短命正常）→ 说明已失效即可，不替换不绕行 |
| 403 | 权限不足 → STOP：按 SKILL 安全硬约束④回复用户等待配置 |
| Issue 定位不到 | `grape git-repo issues get --owner "$REPO_OWNER" --repo "$REPO_NAME" --number "$ISSUE_NUMBER"` / `issues list` 放宽条件 |
| A2A handoff 非 2xx/超时（退出码 ≥400） | 重试 ≤3 次 → 仍失败评论说明「A2A 服务不可达」+ 人工介入标注 |
| CLI 未装/版本过低 | `references/grape-cli-installation.md`：`timeout 300 pip install git+https://gitcode.com/grape-dev/grape-cli.git` → 再验 |
| 长命令篡改 | 长 payload 先写本地文件再 `--json @file`——禁止 shell 内联转义踩坑 |
| `validate_domain_project.py` 路径缺失 | `ls references/scripts/` 确认；缺失 → 从 XXX 仓库按 SKILL 摘取（只复制该脚本文件） |
| 构建因环境输出差异失败 | `timeout 300 npm run build >/dev/null 2>&1` 退出码为判定唯一标准，忽略输出 |
| 重复 A2A | pipeline 已含 `coding:a2a_event` → 不重复，只发评论 |