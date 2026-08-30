---
name: huawei-cloud-skill-basictest
description: |
  对任意华为云 skill 从专业角度执行完整基本功评测，端到端验证被测 skill 的能力边界、触发词、正反例、报错质量，
  测试完成后输出专业测试报告与问题单并归档。
  整合三套成熟评测规范：基本功问题生成（定边界→出正反例→查重→实测→归档→问题整合）、
  huawei-cloud-skill-tester 端到端执行、评测汇总清单生成。
  七阶段流程：准备→定边界→出正例→出反例→查重查归类→实测验证→汇总归档。
  默认全自动无人值守（auto-approve，无人工确认环节），评测收尾自动按资源标识检测并释放测试产生的云资源。
  Triggers include: "基本功评测", "跑skill评测", "测试skill", "跑技能评测", "执行skill评测", "skill评测流程", "run skill evaluation", "skill basictest".
tags: [evaluation, testing, quality, skill, huawei]
---

# 华为云 Skill 基本功评测

> 对任意 skill，从专业测试视角出发：先定能力边界，再生成能触发该 skill 的正例与反例评测问题，
> 使用 `huawei-cloud-skill-tester` 端到端实测验证，最终输出专业测试报告、问题单并归档。

---

## 依赖

- **`huawei-cloud-skill-tester`**：评测执行引擎，提供 `run-test-pipeline.sh --skills <target>` 端到端流水线（7 个 phase：install-check / skill-analysis / tech-research / test-case-generation / test-execution / orchestration / full-flow），产出 `phase-summaries/` 与 `reports/test-report.{json,md}`。**如未安装，评测启动时自动从 clawhub.ai 导入（见"一、准备"步骤 1），无需用户手动安装**
- **批量执行脚本**：`scripts/run-eval-batch.sh`（一次调用跑完正反用例集全部用例：register→执行→判定→complete→归档，省模型轮次）
- **门禁脚本**：`scripts/skill-eval-gate.sh`（逐条用例门禁）+ `scripts/run-skill-eval-gate.sh`（A-H 步骤门禁），默认 `--auto-approve` 无人值守
- **汇总脚本**：`scripts/generate_summary.py`（评测汇总报告）
- **总报告脚本**：`scripts/generate_total_report.py`（合并生成 PR 唯一总测试报告）
- **资源释放脚本**：`scripts/cleanup_resources.sh`（按资源标识检测并释放评测产生的云资源）
- **GitCode 统一接口脚本**：`scripts/gitcode_api.sh`（所有 GitCode 交互的统一封装，禁止散落直接 API 调用）
- **OBS 上传脚本**：`scripts/upload_report_obs.sh`（上传测试报告到 obs-hd-dev-static/testReport/ 并生成回源下载 URL）
- **入库脚本**：`scripts/insert_test_detail.py`（评测结果写入 hd_skill_test_detail 表）

---

## 触发所需信息（全自动推导，无需向用户索取）

本 skill 为**全自动无人值守**模式，所有触发信息自动推导，无人工确认环节：

1. **要评测的 skill 名称**（`{target}`）——从触发内容解析（PR 链接 → 变更的 skill 目录名；直接给出 skill 名则直接使用）
2. **归档地址** `{repo}`——自动取被测 PR 所在仓库（如 `https://gitcode.com/{owner}/{repo}/tree/main`）；无 PR 时取触发时携带的仓库地址，缺省跳过归档
3. **问题单创建地址**——与归档地址一致
4. **资源标识**——自动生成 `{当前用户缩写}{YYYYMMDD}`（如 `dbq20260803`），用户缩写从 multica 当前用户或 `$USER` 推导；涉及创建云资源时命名必须含此标识，资源释放按此标识过滤
5. **归档仓库鉴权凭证**——优先读环境变量 `GITCODE_TOKEN`，不存在则读 `~/.gitcode_token`；均无则跳过需鉴权的操作（建 issue/归档）并在报告中注明。不在对话中暴露凭证值

以下信息由 agent 自行获取，不向用户索取：
- **skill 运行环境信息**（环境变量、配置文件等）；取不到或无法确定的才向用户索取
- **被测 skill 的安装状态**：评测开始前若已安装则先卸载（含备份目录 `~/.skill-eval-backup/{skill}/`）再重新安装，确保干净状态

## GitCode 交互规范（强制）

所有与 GitCode 的交互**必须通过统一封装脚本 `scripts/gitcode_api.sh`**，禁止散落直接 API 调用（curl/POST/PATCH）：

```bash
bash scripts/gitcode_api.sh issue create <owner> <repo> --title "..." --body-file <报告文件> [--assignee <PR创建人login>] [--assignee-from-pr <PR编号>] [--pr-url <PR链接>]   # 有BUG时按7.4创建/复用BUG issue（PR创建人≠skills_bot，责任人用--assignee传PR创建人）
bash scripts/gitcode_api.sh issue get <owner> <repo> <number>
bash scripts/gitcode_api.sh issue list <owner> <repo> [--labels X] [--per-page N] [--page N]
bash scripts/gitcode_api.sh issue find-by-pr <owner> <repo> <PR编号> [--label bug]   # 定位PR的open BUG issue
bash scripts/gitcode_api.sh issue comment <owner> <repo> <number> --body-file <留言>   # 给issue留言
bash scripts/gitcode_api.sh issue close <owner> <repo> <number>   # 关闭issue
bash scripts/gitcode_api.sh issue url-by-pr <owner> <repo> <PR编号>   # 取PR关联需求issue的html_url
bash scripts/gitcode_api.sh issue close-by-pr <owner> <repo> <PR编号>   # 关闭PR关联的需求issue（排除BUG issue）
bash scripts/gitcode_api.sh issue labels-set <owner> <repo> <number> <label...>
bash scripts/gitcode_api.sh pr get <owner> <repo> <number>
bash scripts/gitcode_api.sh pr comment <owner> <repo> <number> --body-file <报告文件>
bash scripts/gitcode_api.sh pr tester-assign <owner> <repo> <number> <username>
bash scripts/gitcode_api.sh pr assignee-assign <owner> <repo> <number> <username>
```

- **PR 链接入来源**：创建 issue 时用 `--pr-url <PR链接>` 自动并入来源行（有 BUG 时按 7.4 走 BUG issue 路径，`--pr-url` 用于 BUG issue 与需求 issue 的来源标注）
- **责任人**：BUG issue 责任人必须是 **PR 创建人**（`pr get` 的 `user.login`），用 `--assignee <login>` 传；`--assignee-from-pr <PR编号>` 只追溯**需求 issue 创建人**（用于手动/其他场景），两者语义不同，BUG 路径不可用 `--assignee-from-pr`

- **中文编码（强制）**：创建 issue / PR 评论等含中文内容时，**必须用 `--body-file` 从文件读取正文**，且脚本以 `ensure_ascii=False` 序列化——保证 GitCode 上显示正常中文。**禁止**在调用侧先 `json.dumps` 预转义（会导致双重编码出现 `\uXXXX` 乱码），也禁止用 `--body` 传超长/含转义的内容
- **Token 统一**：脚本读取 `GITCODE_TOKEN` 环境变量或 `~/.gitcode_token`，调用方不传；**须使用对目标仓库有写权限的 token**（labels 写入、PR 测试人指派等操作依赖 token 所属用户权限）
- **正确接口**：加标签用 `POST /issues/{n}/labels` + 裸数组（添加，幂等：已存在则跳过）；指派测试人用 `POST /pulls/{n}/testers` + body `{"testers":"<username>"}`（注意字段是 testers 不是 assignees）
- **退出码**：非 0 表示操作失败/未生效（如标签权限不足、被指派人无测试人资格），调用方据错误信息决策（记录/降级），**不重试不循环**

---

## 规范自检（强制，贯穿所有阶段）

- **触发后第一优先**：触发词出现后，第一个动作必须是规范自检，不得先执行任何其他动作
- 每个动作执行前对照本规范逐条检查合规性，不合规不得执行
- 自检结果直接展示于执行输出（"规范要求 X，本次动作符合/违反"），合规直接执行
- **无人值守**：不等待用户确认，自动放行；仅当遇到无法自动决策的异常（引擎安装失败、凭证完全缺失且必须鉴权）时才向用户报告并暂停

---

## 一、准备（评测环境初始化）

### 0. 任务起始判断（先于一切操作）

接到任务时，按以下顺序执行：

1. **提取 PR 信息**：从任务信息中提取仓库 `{owner}/{repo}` 和 PR 编号 `{pr_number}`（如 `https://gitcode.com/hd-skills-pre/skills-pre/merge_requests/13` → owner=hd-skills-pre, repo=skills-pre, pr=13）

2. **取 PR 当前 head sha**：通过 GitCode API 获取 PR 最新 head sha：
   ```bash
   bash scripts/gitcode_api.sh pr get {owner} {repo} {pr_number}
   ```
   从返回 JSON 的 `head.sha` 字段提取 `{head_sha}` 值。

3. **查询已有流程记录**：通过需求 issue 关联的 PR 链接匹配 `t_skills_dev_flow.node_repo_url`：
   ```sql
   SELECT f.node_test_status, f.node_test_head_sha, f.node_repo_url
   FROM t_skills_dev_flow f
   JOIN t_skills_dev_requirement r ON r.id = f.requirement_id
   WHERE f.node_repo_url LIKE '%merge_requests/{pr_number}'
      OR f.node_repo_url LIKE '%/pull/{pr_number}'
   LIMIT 1
   ```

4. **若已有流程记录且 `node_test_status` 非空**，执行 head sha 比较：
   - **如果 `node_test_head_sha IS NULL`**（老记录无 sha，可能是升级前的任务）→ 非空状态视为已有测试，判断为重复 → 输出"无需操作（旧记录无 head sha，已存在测试记录）"，记录 `squad_activity=no_action`，设置 issue 状态为 `cancelled`，结束
   - **如果 `node_test_head_sha != '{head_sha}'`**（head sha 变了，PR 有更新）→ **需要重测**：更新 `node_test_head_sha='{新 head_sha}'`，设置 `node_test_status=processing`，**继续按规范执行完整评测流程**
     ```sql
     UPDATE t_skills_dev_flow
     SET node_test_head_sha='{head_sha}', node_test_status='processing'
     WHERE node_repo_url LIKE '%merge_requests/{pr_number}'
     LIMIT 1
     ```
   - **如果 `node_test_head_sha == '{head_sha}'`**（head sha 未变，真正重复）→ 输出"无需操作（PR head sha 未变，已有测试记录）"，记录 `squad_activity=no_action`，设置 issue 状态为 `cancelled`，结束

5. **测试完成时（终态）**：在 7.7 入库时同时更新 `node_test_head_sha`：
   ```sql
   UPDATE t_skills_dev_flow
   SET node_test_head_sha='{head_sha}'
   WHERE node_repo_url LIKE '%merge_requests/{pr_number}'
   LIMIT 1
   ```

1. **检查评测引擎依赖 `huawei-cloud-skill-tester`（强制，先于其他所有准备动作）**：
   - 检查方式：`multica skill list --output json`，确认结果中已含 `huawei-cloud-skill-tester`；已安装则跳过本步
   - 若缺失，自动执行在线安装，不得跳过、不得静默继续：
     ```
     MULTICA_HTTP_TIMEOUT=120 multica skill import \
       --url "https://clawhub.ai/huaweiclouddev/huawei-cloud-skill-tester" \
       --workspace-id <当前工作区> --output json
     ```
   - 安装成功后验证：`multica skill get <skill-id> --output json` 确认 SKILL.md 与 `scripts/run-test-pipeline.sh` 存在
   - **安装失败时**：自动重试一次；仍失败则明确报告失败原因（网络、鉴权、超时等）并**自动降级为纯手动实测**（阶段六仍可完整执行），在最终报告中注明"未使用 huawei-cloud-skill-tester 自动化引擎"；不暂停等待用户
   - **降级路径（联网不可用时）**：放弃 tester 自动化流水线，仅用本 skill 的门禁脚本 + 逐条手动实测（阶段六仍可完整执行），并在最终报告中注明"未使用 huawei-cloud-skill-tester 自动化引擎"
2. 清空本次评测的状态（门禁状态文件、scratchpad 条目、本地临时文件、遗留测试云资源），保留归档仓库已归档内容和被测 skill
3. 检查被测 skill 安装状态：已安装则卸载（含备份目录）后重新安装，确保干净状态（无需用户确认）
4. 检查是否可复用既有正反用例集（按顺序查三层）：
   - 本地备份 `~/.skill-eval-backup/{skill}/正反用例集.md` → 有则**自动复用**（无需询问）
   - 归档仓库 `基路径/{skill}/case-run/` 下任一 `YYYY-MM-DD/` 存在 `正反用例集.md` → 同上自动复用
   - 都没有 → 从阶段二开始
   - 无论用例复用还是新生成，阶段六实测必须重新执行
5. 初始化评测门禁：
   ```
   scripts/run-skill-eval-gate.sh --init --skill {target} --repo {repo}
   ```

---

## 二、定边界（内部完成，不输出）

- 读被测 skill 的 SKILL.md 和所有 references，重点看实际命令/脚本/代码
- 逐个参数对照真实用法确认是否生效（参数存在 ≠ 能力存在）。常见失效模式：
  - 参数存在但硬编码覆盖
  - 参数存在但未传递
  - 参数存在但被拦截
- 区分 skill 职责和 agent 职责：skill 最后一步结束后用户拥有什么 = skill 的产出
- 列出能做和不能做，形成边界清单
- 推导方式标注：读代码得出的标置信度（高/中/低）；实际执行验证的标"已实测"

---

## 三、出正例（skill 能完成的）

- 对照触发词确认能触发 skill
- 用用户原话写，不出现技术术语、步骤名、机制描述
- 期望结果必须用户能自己判断成败
- 不同入参方式各一条，同一句话换内部状态不算新类型
- 涉及创建云资源的，资源名称必须包含资源标识
- **触发词验证（强制门禁）**：每条用例生成后输出触发词对照表（用例编号 | 用户原话 | 命中的触发词），未命中的修正；对照表未输出的不得进入实测

---

## 四、出反例（能触发 skill 但办不到的）

- 对照触发词确认能触发 skill
- **反例推导来源（强制）**：从权威来源获取被评测服务的完整能力清单（如 `hcloud <service> --help` 展开所有子命令、API 文档），用完整清单减去 skill 能做的，差集即为反例候选
- 逐个排查前置条件、输入校验、参数实现，每发现一个"触发但失败"的点出一条
- 测的是**报错质量**：skill 办不到时用户拿到的反馈有没有用
  - 好：明确告诉用户哪里错了、怎么改
  - 一般：有报错但没说到点上
  - 差：无任何提示，用户被静默忽略
- 每条反例标注失败原因的推导依据和置信度
- **改进点要求**：治本不治标，针对根因给兜底方案（多源兜底、失败时明确告知）
- 触发词验证门禁同上

---

## 四.5 数据依赖分支覆盖（强制）

评测用例必须覆盖**数据依赖分支**，不能只依赖真实账号返回的空数据——否则隐藏在"有数据才执行"的分支内的 BUG（崩溃路径、类型序列化等）无法被发现。典型事故：账号 `total_count=0` 时 `for record in records:` 循环体不执行，`record.type` 字段不存在、Decimal 金额序列化等崩溃路径全部测不出，被 verify 环节构造数据才暴露。

- **强制走数据分支（强制）**：对存在数据依赖分支的查询/列表/明细/汇总/金额类命令，用例必须覆盖**有数据**场景——
  - 优先用**有真实数据的账号/资源**执行（先探测是否存在数据；无数据则明确标注"无数据，分支未覆盖"）
  - 真实数据不足时，用 **SDK 模型/mock 构造数据**覆盖：直接构造"有记录、有金额、边界数据"的对象驱动被测代码路径（构造类用例在编号后标注 `(构造数据)`）
- **逐条检查数据分支**：每条涉及列表/明细/汇总/金额的用例，确认其"空数据 vs 有数据"两条分支是否都被覆盖
- **输出标注**：用例集输出时，对未能覆盖数据分支的用例标注 `[分支未覆盖: 无数据]`，供审阅/后续轮次补充
- **实测记录**：确因账号无数据且无法构造的，必须在问题单中记录"该数据分支未覆盖"，不得据此误判为无 BUG

---

## 五、查重和查归类

- 查重：用户说出来是一样的 = 重复，删
- 查归类：归因相同的归同一类，不按表面现象分
- **因果链检查**：同一因果链上的环节（A→B→C→用户被误导）合并为一条，合并后改进点覆盖链条上每个环节
- **同类≠同一问题**：是否合并取决于是否构成同一用户体验，不取决于缺陷分类
- **本地备份（强制）**：用例集定稿后立即备份到 `~/.skill-eval-backup/{skill}/正反用例集.md`（含每条用例的用户原话、触发词、预期行为；反例另含预测报错质量、改进点）

---

## 六、实测验证（核心执行）

### 6.1 用例确认与执行顺序

- 正反例**自动生成并自动进入实测**，无需用户审阅
- agent **自动**确定执行顺序：正例在前反例在后；正例内只读查询→创建→复用查询→清理；反例按归因分组；依赖驱动的用例前置于被依赖用例
- 前置资源在正例第一条写操作前创建，全部跑完统一清理

### 6.2 门禁（每条用例强制，默认自动放行）

**首选批量执行（省模型轮次，强制）**：完整用例集用 `scripts/run-eval-batch.sh` 一次跑完（内部自动对每条执行 register→执行→判定→complete→归档）：
0. **自动生成用例与命令映射**（1 次调用，替代逐条思考）：
   ```
   bash scripts/prepare-eval.sh --skill-dir <skill目录> [--output-dir <输出>] [--resource-check "<只读命令>"]
   ```
   脚本从 SKILL.md 提取命令/触发词，自动生成 `正反用例集.md`（P=每命令一条正例 + N=标准越界/输入校验反例）与 `cases-commands.json`
1. **模型审阅微调**（1 次调用）：核对/修正生成的用例与命令（尤其反例的判定命令），必要时补充复杂场景用例
2. **跑批量**（1 次调用）：
   ```
   bash scripts/run-eval-batch.sh --skill {target} --cases <正反用例集.md> --commands <cases-commands.json> --resource-tag <tag> --archive-dir <归档基路径>
   ```
3. 批量脚本输出每条结果汇总（pass/warn/fail + 输出片段），据此生成问题单与整合报告

批量不适用时（依赖用例间传递、需逐条人工判断的复杂用例）才逐条走下方门禁：

每条用例执行前必须走门禁脚本 `scripts/skill-eval-gate.sh`（**默认 `--auto-approve` 无人值守**，register 时自动确认放行）：

1. **注册**：`--register --skill <name> --case <id> --input <text> --trigger <words> --type <positive|negative> --resource-tag <tag>`（register 输出直接展示于执行日志，自动确认，不阻断）
2. **自动放行**：默认 register 后状态自动流转为 `confirmed`（auto-approve）；如需人工确认模式，加 `--interactive` 后 `--confirm --case <id>` 手动放行
3. **执行完成后记录结果**：`--complete --case <id> --result <pass|warn|fail>`
4. **归档后校验格式**：`--validate-archive --archive-dir <YYYY-MM-DD目录路径>`
5. **全部跑完后审计**：`--audit --skill <name>`
6. **清理状态文件**：`--cleanup --skill <name>`
7. **中断恢复检查**：`--resume-check --skill <name>`

硬性要求：
- complete 时用例状态非 confirmed 自动标记 VIOLATION（审计可见）
- 门禁脚本在 register 时强制检查前序用例全部 completed，从脚本层面防止跳步
- **无人值守：逐条自动串行执行，无需用户逐条确认**（仅中断恢复时先运行 `--resume-check`）
- 执行过程中全程输出执行日志

### 6.3 执行方式

逐条串行执行，同一次测试会话内状态累积传递（模拟同一用户连续提问）；前一条产生的资源和结果信息后一条可直接复用。

**执行引擎优先级**：
1. **批量脚本**（6.2 首选，省模型轮次）：`run-eval-batch.sh` 一次跑完正反用例集全部用例，agent 只做"生成 commands JSON + 跑批 + 解析汇总"
2. `huawei-cloud-skill-tester` 端到端流水线：
```
run-test-pipeline.sh --skills {target}
```
评测完成后解析其产出：`phase-summaries/phase-{0..7}-summary.json`、`reports/test-report.json`（overall_statistics + skills_tested）、`reports/test-report.md`。

**执行硬约束**：
- 不伪造产出：所有评测文件必须是实测跑出来的
- 不改 tester 原始产出：phase 摘要、reports 一律不得修改
- 可自修环境/配置问题，但仅限环境层面
- 全程记录遇到的报错、中断、异常
- 资源标识验证（强制）：执行后检查所有云资源是否含资源标识，未标识的立即清理并标注
- 不动老基线（强制）：只操作带当前资源标识的资源
- 查询按标识过滤（强制）：查找带有资源标识的资源作测试对象

### 6.4 对比规则

| 预测 | 实测 | 处理 |
|------|------|------|
| 好 | 好 | 达标 |
| 好 | 一般/差 | 不达标，生成问题单，标注推导误判 |
| 一般 | 好 | 达标，回溯修正推导方法 |
| 一般 | 一般/差 | 不达标，生成问题单 |
| 差 | 好 | 达标，回溯修正推导方法 |
| 差 | 一般/差 | 不达标，生成问题单 |

### 6.5 逐条归档（强制）

每条用例跑完立即归档，不等全部跑完：
```
仓库根目录/{target}/case-run/YYYY-MM-DD/{编号}_{场景描述}_{状态}/
    ├── 原始报告或输出文件
    ├── 改进版报告（如有）
    ├── 问题单（如有）
    └── 用例说明.md
```
- 目录命名：`{编号}_{场景描述}_{状态}`，如 `N1_镜像复制策略_失败`、`P2_MaaS模型安装_告警`；状态取值：通过/告警/失败
- **归档后必须校验**：运行 `skill-eval-gate.sh --validate-archive --archive-dir <YYYY-MM-DD目录>`，退出码 1 必须当场修复重新校验，通过后方可继续下一条用例
- `用例说明.md` 模板见下方"问题单与用例说明模板"

---

## 七、汇总与归档

### 7.0 资源释放（强制，无论测试结果）

评测收尾阶段**必须执行资源检测与释放**，无论是否发现 BUG、测试是否通过。使用 `scripts/cleanup_resources.sh`：

```bash
bash scripts/cleanup_resources.sh --resource-tag {资源标识} --region cn-north-4 --report {日期目录}
```

- **检测范围**：按资源标识过滤 ECS 实例 / 弹性公网IP(EIP) / 安全组 / IP地址组 / ELB 负载均衡
- **释放规则**：仅释放名称含资源标识的资源，不动无关资源
- **产物**：生成 `资源释放报告.md`（扫描到 / 释放成功 / 失败明细），归档到 case-run 日期目录
- **无凭证环境**：脚本输出警告并跳过（报告注明），不阻塞评测
- **释放失败**：报告列出失败资源与原因，不静默结束
- **dry-run 支持**：`--dry-run` 仅检测不释放（排查用）

> 在 7.1 问题整合开始前执行，确保归档前资源已清理。

### 7.1 问题整合

1. 将本 skill 全部评测用例发现的问题整合为一张问题单（按问题单规范）
2. 去重：相同问题只保留一条；因果链上的环节合并
3. 生成整合测试报告：整合过程说明 + 去重确认 + 修正确认（每条都有明确的改进点且能修正到"好"）

### 7.2 汇总报告生成

使用 `scripts/generate_summary.py` 生成汇总报告：
```
python3 scripts/generate_summary.py \
  --eval-dir <evaluation-results目录> \
  --output-dir <输出目录> \
  --issue-owner {owner} --issue-repo {repo}
```
- 单 skill 汇总：`{skill}_summary.md`
- 总览汇总：`总览_summary.md`
- 报告格式规范见 `references/report-format.md`

### 7.3 归档

整合测试报告与问题单统一归档到 `基路径/{target}/case-run/YYYY-MM-DD/`：
```
基路径/{target}/case-run/YYYY-MM-DD/
    ├── 整合测试报告.md
    ├── 正反用例集.md
    ├── 触发词归档.md
    ├── 资源释放报告.md
    └── {编号}_{场景描述}_{状态}/...
```
路径解析规则：归档地址含 `{target}/case-run/` → 截取该段之前为基路径；含 `{target}` 无 `case-run` → 截取 `{target}` 之前；都不含 → 整个地址为基路径。

### 7.4 BUG 记录（强制，不创建 BUG issue）

**有 BUG 时**：**不再创建 GitCode BUG issue**（无论 PR 创建人是谁）。BUG 检测结果直接入表追踪（`hd_skill_test_detail` 主表 total/resolved + `hd_skill_bug_detail` 子表，见 7.7），并通过上传到 PR 的总测试报告（7.5/7.6）告知开发者。
- 历史遗留的 open BUG issue（此前版本创建）在重测闭环（7.8）中按既有逻辑处理

**无 BUG 时**：若该 PR 存在**历史遗留** open 的 BUG issue（此前创建，说明 BUG 已修复），关闭它并留言"BUG 已修复"（用 `issue close` + `issue comment`）；**不关闭需求 issue**（需求 issue 保持原状态，与 BUG issue 独立）

关闭失败（无凭证/网络/仓库不存在）不阻塞流程，在报告中注明原因
- 归档完成后生成 `触发词归档.md`（每条用例的用户原话、触发词、归档目录名）

### 7.5 总测试报告（一个 PR 一份，强制）

评测收尾**必须生成一份 PR 唯一总测试报告**，合并整合测试报告、问题单、正反用例集为一份，作为上传到 PR 的唯一报告：

```bash
python3 scripts/generate_total_report.py \
  --skill {target} --case-run {日期目录} \
  --pr-url {PR链接} --output {日期目录}
```

- **产出**：`{target}_总测试报告.md`（覆盖式，同一 PR 重复运行只保留一份，不新增多份）
- **内容**：测试统计（总用例/通过/告警/失败/通过率）+ 问题清单（严重级别+改进点）+ 详细整合报告 + 正反用例集
- **强制规则**：一个 PR 只允许一份总测试报告；上传 PR 时以此报告为唯一内容，不再上传其他多份报告
- **有 BUG 且 PR 创建人 ≠ skills_bot（@PR 提交人，强制）**：上传 PR 报告评论时用 `--prepend "@{PR创建人login} 评测发现 BUG，请查看下方总测试报告并修复。"`，让外部 PR 提交人在 GitCode 收到通知（无 BUG issue 可指派，靠此 @ 通知）
- **下游通知（强制，无 BUG 时）**：测试通过（`node_test_status=done`）后，在任务 issue 评论 @skills_verify 并**带上 PR 链接**，唤醒验证节点（node_verify）：
  ```
  [@skills_verify](mention://squad/3d374d07-a21d-4ab9-a95c-6f3b8a5469f7)
  请对 PR {PR链接}（{skill 名称}）进行验证。
  ```
  通过 `multica issue comment add` 发布；PR 链接用 `https://gitcode.com/<owner>/<repo>/merge_requests/<number>` 或 `/pull/<number>`

### 7.6 测试报告上传 OBS（强制）

总测试报告生成后**必须转成 PDF 并上传到 OBS 桶**，生成可下载的回源 URL：

```bash
HUAWEI_ACCESS_KEY=<AK> HUAWEI_SECRET_KEY=<SK> \
  bash scripts/upload_report_obs.sh {总报告文件.md} --skill {target} --date {YYYY-MM-DD}
```

- **流程**：`upload_report_obs.sh` 先用 `md-to-pdf`(Chromium) 把 MD 转 PDF，再上传
- **桶**：`obs-hd-dev-static`，路径 `testReport/`，对象名 `{skill}_{日期}_总测试报告.pdf`
- **下载 URL**：`http://skills.topxtopx.com/testReport/{skill}_{日期}_总测试报告.pdf`（私有桶回源域名）
- **凭证**：`HUAWEI_ACCESS_KEY` / `HUAWEI_SECRET_KEY` 环境变量，不硬编码
- **依赖**：`md-to-pdf`（npm 全局）+ Chromium（默认 Playwright chromium，可用 `CHROME_BIN` 覆盖）+ 中文字体
- **产出**：脚本输出回源下载 URL（.pdf），写入归档（整合测试报告附链接）、总测试报告头部、`hd_skill_test_detail.report_url`
- 转换/上传失败不阻塞评测（记录原因，报告注明无下载链接）

### 7.7 测试结果入库（强制）

评测收尾**必须将测试结果写入 `hd_skill_test_detail` 表**：

```bash
python3 scripts/insert_test_detail.py \
  --skill {target} --case-run {日期目录} \
  --report-url {OBS回源URL} --pr {PR编号} --repo {owner}/{repo} --owner {负责人} \
  [--bug-issue-url {BUG issue URL}]
```

> **`--repo` 必传（强制）**：必须传**当前 PR 所在仓库**（如 `g30074593/skillspackage`）。脚本默认值为 `developer-skill/huaweicloud-skills-dev`，漏传会在错误仓库反推需求 issue → 产生 `pr:{编号}` 脏记录。

**BUG 统计命令**：`bash scripts/gitcode_api.sh issue bugs-by-pr <owner> <repo> <PR编号>`，输出 `{"total":N,"closed":M}`，统计该 PR 全部 BUG issue（含 closed，标题含 `PR #<编号>` 或 body 含 `merge_requests/<编号>` 匹配）。

**子表 BUG 详情入库（强制）**：主表入库后，从问题单解析每个 BUG 详情写入 `hd_skill_bug_detail`（关联主表 `issue_id`）：
- 字段：`seq_no`（BUG 序号，如 BUG-001，优先从编号 ISSUE-NNN 提取）、`severity`（高/中/低，HIGH→高/MEDIUM→中/LOW→低）、`problem`（问题标题）、`resolved`（问题块内"状态: 已修复 或 ✅ 已修复"→1，否则 0）
- 复测幂等：按 `(issue_id, seq_no)` 匹配更新已有行 + 新 seq_no 插入，不删除已有行
- 子表失败不阻塞主表入库（记录原因）

**一 issue 一条（UPSERT）**：以 `issue_id` 为唯一键，同一 issue 的多次测试（含复测）更新同一条记录，不新增行。

**字段来源**：

| 字段 | 来源 |
|------|------|
| `skill_name` | 被测 skill 名 |
| `cloud_service` | skill 名提取服务代码（`huawei-cloud-{svc}-xxx` → `svc`） |
| `cloud_service_name` | `references/skill-service-map.md` 映射（缺省用服务代码大写） |
| `issue_id` | PR 反推需求 issue 的 id；反推不到 → `pr:{PR编号}`（PR 当 issue） |
| `issue_name` | PR 反推需求 issue 的 title；反推不到 → PR 标题 |
| `issue_url` | 有 BUG 且已创建 BUG issue → 该 BUG issue 的 html_url（`--bug-issue-url`）；否则 PR 反推需求 issue 的 html_url（无则 PR 链接） |
| `test_time` | 测试完成当前时间 |
| `passed` | `resolved_count == total_count` → 1；否则 0（无 BUG 时 total=0 → 1） |
| `total_count` | `gitcode_api.sh issue bugs-by-pr` 统计该 PR 所有 BUG issue（open+closed） |
| `resolved_count` | 该 PR 已 closed 的 BUG issue 数（复测解决数） |
| `owner` | 有需求 issue → issue 创建人；无 issue（PR 当 issue）→ PR 创建人；均失败用 `--owner` 兜底 |
| `report_url` | OBS 回源下载 URL |
| `id` / `created_at` | 数据库自动 |

- 写入失败不阻塞评测（记录原因）

**node_repo 置 processing（仅无 BUG，首次与重测均适用，幂等）**：无 BUG 测试完成（`node_test_status=done`）时，将共建仓库节点置为"入库处理中"——**设置前先查询 `node_repo_status`，仅当其仍为初始值 `pending`（未开始）时更新**，其他值（processing/done/skipped 等）跳过：
```sql
UPDATE t_skills_dev_flow
SET node_repo_status='processing',
    node_repo_url='https://gitcode.com/{owner}/{repo}/merge_requests/{pr_number}',  -- 当前 PR 链接
    node_repo_time=NOW()
WHERE multica_issue_id='{multica_issue_id}'
  AND node_repo_status='pending'
```
- 注意：`node_repo_status` 列 NOT NULL 默认 `pending`，故判空实为"是否仍为 pending"；`node_repo_url` 用**当前 PR 链接**（`https://gitcode.com/{owner}/{repo}/merge_requests/{pr_number}`）；执行后确认影响行数为 1
- 有 BUG 时绝不得置 node_repo；该节点最终由 PR 合入事件置 `done`（事件处理器既有逻辑）

**设置 PR 审查人与合并人（无条件，与测试结果无关；PR 创建时已由 skill-auto-test-pr 设置，此处为幂等安全网）**：无论测试是否通过，确保 PR 审查人（v5 `assignees`，login）与合并人（内网 API `assignee_ids`，数字 id）已设置：
```bash
# 审查人（v5 API，login）——必须为目标仓库 Owner/Maintainer 级（默认 g30074593）
bash scripts/gitcode_api.sh pr assignee-assign <owner> <repo> <pr_number> "${GITCODE_PR_ASSIGNEE:-g30074593}"

# 合并人（内网 API，数字 id）——默认 GITCODE_PR_ASSIGNEE_ID=11027048(g30074593)
GITCODE_ACCESS_TOKEN="$GITCODE_ACCESS_TOKEN" GITCODE_TOKEN="$GITCODE_TOKEN" \
  GITCODE_PR_ASSIGNEE_ID="${GITCODE_PR_ASSIGNEE_ID:-11027048}" \
  python3 scripts/set_pr_merger.py "https://gitcode.com/{owner}/{repo}/merge_requests/{pr_number}"
```
- 审查人与合并人是**两个独立字段**：审查人=v5 `assignees`(login 字符串)，合并人=内网 `assignee_ids`(数字用户 id)
- 依赖环境变量：`GITCODE_TOKEN`、`GITCODE_ACCESS_TOKEN`、`GITCODE_PR_ASSIGNEE_ID`（已注入 skills_test agent env）
- **失败不阻塞评测**（记录原因）；重复设置幂等（脚本回读确认，不误报）

### 7.8 重测与 BUG 闭环（重测任务时强制）

**触发条件**：本任务为重测任务（Multica 任务描述含「重测」标记，或该 PR 存在 open 的 BUG issue）。

**流程**：
1. **复测开始态（强制，先于一切评测动作）**：将状态更新为开始态——`t_skills_dev_flow.node_test_status=processing` + `t_skills_dev_requirement.status=testing`（把上次的 failed 状态改回"测试中"），再开始重测
2. 全量重跑本评测流程（与首次一致），生成问题单
3. 用 `gitcode_api.sh issue find-by-pr <owner> <repo> <PR编号> --label bug` 列出该 PR 的 open BUG issue
4. 对每个 open BUG issue 判定原 BUG 是否仍复现（按原 issue 问题单逐条比对最新问题单）：
   - **仍复现** → 写留言到临时文件后 `gitcode_api.sh issue comment <owner> <repo> <n> --body-file <文件>`（内容："该 BUG 还未修复，最新复现信息：..."；幂等：同一 BUG 已留言"还未修复"则更新为最新证据，不重复刷屏；中文必须走 `--body-file`）
   - **未复现（已修复）** → `gitcode_api.sh issue close <owner> <repo> <n>` 关闭该 BUG issue，并用 `issue comment ... --body-file` 留言"BUG 已修复"
5. 重测结果照常：总测试报告上传 PR（7.5）、指派测试人 wj911831、测试结果入库（7.7）
6. **重测任务不新建 BUG issue**（仅复用/关闭已有）
7. **复测终态（与首次规则一致）**：仍复现 → `node_test_status=failed` + `status=failed`；全部修复 → `node_test_status=done` + `status=tested`，并按 7.7「node_repo 置 processing 规则」置 `node_repo`（先判空，非空跳过），**并按 7.5 下游通知 @skills_verify + PR 链接唤醒验证节点**

---

## 问题单与用例说明模板

### 问题单字段

| 字段 | 说明 |
|------|------|
| 问题标题 | 一句话描述问题 |
| 触发输入 | 用户原话 |
| 预期行为 | skill 应该做到什么 |
| 实际行为 | skill 实际做了什么 |
| 报错质量 | 好/一般/差 |
| 具体问题 | 哪段逻辑/哪个参数/哪步处理出了问题，必须明确不模棱两可 |
| 影响分析 | 对用户造成什么影响 |
| 改进点 | 怎么改、改完后预期效果，治本不治标 |

**问题属性字段**：

| 字段 | 说明 |
|------|------|
| 编号 | `{SKILL-NAME}-ISSUE-{三位序号}`，从 001 开始 |
| 严重级别 | HIGH（用户被误导造成损失）/ MEDIUM（用户卡住无法继续）/ LOW（体验不佳但可自行解决） |
| 缺陷分类 | 文档缺口 / 逻辑错误 / 安全问题 / 性能问题 / 兼容性问题 |
| 发现来源 | 在哪个阶段、哪些用例发现的 |
| 复现次数 | 稳定复现 / 偶现 |

### 问题单 MD 格式

```
# 问题单: {被评测skill名称}

- 来源: {评测来源}
- 测试日期: {YYYY-MM-DD}
- 被评测 skill: {skill名称}
- 评测方式: {评测方式描述}
- 问题数量: {N}
- 缺陷分类: {分类} {数量}

---

## 问题 {N}: {问题标题}

| 字段 | 内容 |
|------|------|
| 编号 | {SKILL-NAME}-ISSUE-{序号} |
| 严重级别 | HIGH / MEDIUM / LOW |
| 缺陷分类 | 文档缺口 / 逻辑错误 / ... |
| 发现来源 | {...} |
| 复现次数 | {...} |

### 触发输入

> {用户原话}

### 预期行为
### 实际行为
### 报错质量
### 具体问题
### 影响分析
### 改进点

---

## 汇总

| 编号 | 严重级别 | 缺陷分类 | 问题 | 报错质量 | 状态 |
|------|----------|---------|------|---------|------|
| {编号} | {级别} | {分类} | {问题简述} | {质量} | 待修复 |

### 按严重级别统计
### 按报错质量统计
```

### 用例说明.md 格式

```
# 用例说明: {编号} — {场景描述}

- 用例类型: 正例 / 反例
- 用例编号: {编号}
- 测试日期: {YYYY-MM-DD}
- 状态: 通过 / 告警 / 失败

---

## 用户原话

> {用户原话}

## 触发验证

| 项目 | 内容 |
|------|------|
| 命中的 SKILL.md 触发词 | `{触发词}` |
| 是否命中 | ✅ 是 / ❌ 否 |

## 预期行为
## 实际行为
## 报错质量
## 测试结果

{✅ 通过 / ⚠️ 部分通过 / ❌ 失败}
```

### 问题定性要求

基于可观察的用户体验描述问题（用户看到什么、不知道什么），不推测用户心理（如"用户可能产生虚假安全感"）。

---

## 评测汇总报告（测试报告）

评测完成后输出的专业测试报告须涵盖：
- 预期 vs 实际对比
- 报错质量评估
- 发现的问题：整改方案及整改后预期评测质量
- 测试统计（tester 流水线 overall_statistics + case-run 用例统计）
- 通过率 = (通过 + 告警) / 总用例数 × 100%

---

## 注意事项

1. **无人值守（强制）**：本 skill 默认全自动无人值守，无人工确认环节；遇到无法自动决策的异常（引擎安装失败、鉴权凭证完全缺失）才暂停并向用户报告
2. **不伪造产出**：所有文件必须实测跑出来
3. **不动 tester 原始产出**：优化报告是新建文件，不是修改原文件
4. **命名校验强制**：归档文件必须对照模板，校验不通过当场修正并同步更新内部引用
5. **Token 安全**：不将 Token 硬编码到脚本或提交到仓库，使用环境变量 `GITCODE_TOKEN`
6. **GitCode API 限流**：大量 Issue 时建议分页获取
7. **打断恢复**：流程被打断后重新继续时，先运行 `--resume-check` 检查未完成用例，不得直接推断可执行下一条
8. **资源释放强制**：评测收尾必须执行 `scripts/cleanup_resources.sh` 释放带资源标识的云资源，无论测试是否发现 BUG
