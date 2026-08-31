# skills_dev — 华为云 Skill 开发工具链

本目录承载 Multica 平台上"华为云 Skill 开发"的完整自动化工具链，支持从需求接入到自动测试、PR 提交、数据上报的全流程。

---

## 目录结构

```
skills_dev/
├── README.md                      本说明
├── scripts/                       核心脚本（集中引用）
│   ├── feature-gate.sh            受理门禁：仅 feature 类型 issue 可开发
│   ├── pre-create-hook.sh         任务受理即置 node_dev_status=processing
│   ├── report-dev-processing.sh   上报开发中状态
│   ├── report-dev-done.sh         上报开发完成状态
│   ├── report-dev-failed.sh       上报开发失败状态
│   ├── run-pipeline.sh            自动流水线主脚本（tester+PR+DB+telemetry）
│   ├── process-skill.sh           流水线入口
│   └── report.mjs                 遥测上报 CLI（复用 skill-reporter 逻辑）
└── skills/                        工具链 Skills（一键部署优先到 ~/.hermes/skills/huawei-cloud/，兼容 ~/.agents/skills/）
    ├── huawei-cloud-skill-creator       Skill 创建器（六阶段开发）
    ├── huawei-cloud-skill-tester        Skill 测试器（八阶段测试）
    ├── huawei-cloud-skill-reporter      Skill 使用遥测上报
    ├── huawei-cloud-find-skills         Skill 发现
    ├── skill-auto-test-pr               自动测试+PR 流水线
    └── find-skills                      Skill 搜索
```

---

## 架构

```
GitCode 仓库 ──issue webhook──▶ Multica 工作区 ──▶ creator 智能体 ──▶ 提交 PR
     ▲                          (270d4faf)         │受理→开发→自测       │
     │ PR (Fixes #issue)                            ▼                     │
     └───────────────────────────▲── webhook skill-created ─────────────┘
                                 │
                     autopilot (skill-auto-test-pr-pipeline)
                 tester 测试 → PR(幂等+审查人) → DB 更新 → telemetry 上报
```

### 组件依赖

| 组件 | 说明 |
|---|---|
| Multica CLI | `multica`（agent 运行时，daemon 拉起 opencode） |
| GitCode | 目标仓库 + API token（内置在 run-pipeline.sh） |
| 数据库 | `192.168.1.18/rds-hd-skillsopr-db`（t_skills_dev_requirement / t_skills_dev_flow / hd_skill_telemetry） |
| 上报服务 | `skillsop.topxtopx.com/api/telemetry` |
| 运行时 | Python3 + pymysql、jq、git、curl、Node.js（reporter） |

---

## 部署

### 0. 一键部署（推荐，新机器用）

```bash
# 拉取部署脚本（SSH 或 HTTPS）
git clone git@gitcode.com:developer-skill/skillsopr.git /tmp/skillsopr
# 或: git clone https://gitcode.com/developer-skill/skillsopr.git /tmp/skillsopr

# 运行部署（私有仓库若 SSH 不通，先 export GITCODE_TOKEN）
bash /tmp/skillsopr/tools/skills_dev/deploy-skills-dev.sh

# 可选环境变量：MULTICA_WORKSPACE_ID / MULTICA_TOKEN / GITCODE_TOKEN
```

脚本会：检测/安装系统依赖 → 拉取工具链 skills → **优先部署到 Hermes skills（`~/.hermes/skills/huawei-cloud/`，分类可用 `HERMES_SKILLS_CATEGORY` 覆盖）** → 同时兼容部署到 `~/.agents/skills/`（multica agent 环境，设 `SKIP_AGENTS_SKILLS=1` 可跳过）→ 部署核心脚本 → 配置 multica profile → 配置 reporter → **生成 `.env` 配置模板** → 检查 GitCode SSH → 自动验证。

### ⚠️ 部署后必做：填写 `.env`

**新机器环境不同，部署后必须编辑 `~/.agents/skills/.env` 填写实际配置**：

```bash
vi ~/.agents/skills/.env
# 填写: GITCODE_TOKEN / GITCODE_REPO_SSH / GITCODE_OWNER / GITCODE_REPO / GITCODE_CHECKOUT
#       DB_HOST / DB_USER / DB_PASS / DB_NAME
#       HUAWEICLOUD_SDK_AK / HUAWEICLOUD_SDK_SK
```

- 配置优先级：**环境变量 > `.env` 文件 > 无默认值（缺失报错）**
- 脚本不再内置旧环境默认值，缺失必填配置会明确报错提示
- 不写入 `~/.bashrc`（daemon 拉起的 agent 不 source bashrc，且避免污染全局环境）
- 工具链脚本（run-pipeline / feature-gate / pre-create-hook / report-dev-*）都会自动 source `.env`

### 1. 部署 Skills 到 agent 环境（手动）

一键部署脚本默认**优先部署到 Hermes skills**（`~/.hermes/skills/<category>/`，默认分类 `huawei-cloud`），同时兼容部署到 `~/.agents/skills/`（multica agent 环境）。手动部署：

```bash
# Hermes skills（优先，按分类目录）
HERMES_CAT=huawei-cloud
mkdir -p ~/.hermes/skills/$HERMES_CAT
cp -r skills/* ~/.hermes/skills/$HERMES_CAT/

# agents 环境（multica，可选）
SKILLS_BASE=~/.agents/skills
mkdir -p $SKILLS_BASE
cp -r skills/* $SKILLS_BASE/
```

### 2. 配置认证与环境

```bash
# ~/.multica/config.json
# {
#   "server_url": "https://multica.clouddeveloper.club",
#   "workspace_id": "270d4faf-4e8b-4d04-b8d3-ed88569abf4c",
#   "token": "<multica token>"
# }

# ~/.bashrc：华为云 AK/SK（tester 真实环境测试用）
export HUAWEICLOUD_SDK_AK=***
export HUAWEICLOUD_SDK_SK=***

# ~/.config/huawei-cloud-skill-reporter/server_url.txt
echo "http://skillsop.topxtopx.com/api/telemetry" > ~/.config/huawei-cloud-skill-reporter/server_url.txt

# GitCode SSH 免密（skills 推送用）
ssh -T git@gitcode.com
```

### 3. 配置 creator 智能体指令

在 Multica 服务端为 creator 智能体（`Skills开发、测试、PR智能体`）配置受理流程指令（见下方"智能体指令"）。

---

## 工作流程

### 第二阶段：Skill 开发（creator 智能体）

```
1. 受理门禁（feature 检查）      feature-gate.sh
2. 状态即时上报（processing）     pre-create-hook.sh
3. 六阶段开发
4. 完成状态上报（done）          report-dev-done.sh
5. 动态仓库解析
6. 提交 PR + 审查人
7. webhook 通知
```

### 第三阶段：自动流水线（autopilot）

```
process-skill.sh <skill_name> <multica_issue_id>
  ├─ resolve_issue_id
  ├─ resolve_repo_from_issue（三级：DB gitcode_issue_url → 描述"来源仓库" → 默认仓库）
  ├─ update_dev_status processing
  ├─ Phase 1: tester 八阶段测试
  ├─ Phase 2: PR 创建（幂等复用 + 审查人 HugLovesWine）
  └─ Phase 3: DB done/failed + PR 编号入库 + telemetry 上报
```

---

## 核心脚本说明

| 脚本 | 作用 | 关键逻辑 |
|---|---|---|
| `feature-gate.sh` | 受理门禁 | 查 DB gitcode_issue_url → GitCode API 查 issue labels → 无 `feature` 则置 `node_dev_status=failed` 并 exit 2 |
| `pre-create-hook.sh` | 即时状态 | 任务受理即置 `node_dev_status=processing` |
| `report-dev-*.sh` | 状态上报 | done / failed / processing 直写 DB |
| `run-pipeline.sh` | 主流水线 | 三级仓库解析、tester、幂等 PR、审查人、DB 更新、telemetry |
| `report.mjs` | 遥测 CLI | 复用 reporter 的 IP/URL/payload 逻辑，fire-and-forget |

### 关键机制

- **feature 门禁**：非 feature 类型 issue 受理即 failed，不开发
- **即时 processing**：任务受理立即显示"开发中"
- **动态仓库三级解析**：DB `gitcode_issue_url` → 描述 `**来源仓库：**` → 默认 `developer-skill/huaweicloud-skills-dev`
- **PR 幂等**：`find_existing_pr` 按 head.ref 复用已有 PR，防双路径重复
- **PR 审查人**：`POST .../pulls/{n}/assignees` `{"assignees":"HugLovesWine"}`
- **tester 源保护**：源==安装目录时快照恢复，不删源
- **telemetry 复用**：上报逻辑单一来源（reporter 包）

---

## 配置项

### GitCode 仓库（run-pipeline.sh，环境变量可覆盖）

| 变量 | 默认值 |
|---|---|
| `GITCODE_REPO_SSH` | `git@gitcode.com:developer-skill/huaweicloud-skills-dev` |
| `GITCODE_API` | `https://api.gitcode.com/api/v5` |
| `GITCODE_TOKEN` | （内置） |
| `GITCODE_OWNER` | `developer-skill` |
| `GITCODE_REPO` | `huaweicloud-skills-dev` |
| `GITCODE_CHECKOUT` | `huaweicloud-skills-dev` |
| `GITCODE_PR_ASSIGNEE` | `HugLovesWine`（PR 审查人） |

### 数据库（脚本内置）

| 配置 | 值 |
|---|---|
| Host | `192.168.1.18` |
| DB | `rds-hd-skillsopr-db` |
| User | `root` |

### 上报

| 配置 | 值 |
|---|---|
| 默认地址 | `http://skillsop.topxtopx.com/api/telemetry` |
| 覆盖 | `HUAWEI_CLOUD_SKILL_REPORT_URL` |

---

## GitCode PR 角色接口（调研结论）

| 角色 | 接口 | 认证 | 状态 |
|---|---|---|---|
| 审查人 | `POST /api/v5/.../pulls/{n}/assignees` | PRIVATE-TOKEN | ✅ 可用（脚本已用） |
| 评审人 | `POST /api/v5/.../pulls/{n}/reviewers` | PRIVATE-TOKEN | ✅ 可用 |
| 测试人 | `POST /api/v5/.../pulls/{n}/testers` | PRIVATE-TOKEN | ✅ 可用 |
| 标签 | `POST /api/v5/.../pulls/{n}/labels` | PRIVATE-TOKEN | ✅ 可用 |
| 合并 | `PUT /api/v5/.../pulls/{n}/merge` | PRIVATE-TOKEN | ✅ 可用 |
| 合并人（指定） | 仅 web-api 内部接口 `isource/merge_requests/{iid}` | 网页 JWT | ❌ 脚本不可用 |

> 公开 API（api/v5）无"指定合并人"接口；网页端"合并人"走内部 web-api（需登录态 JWT + 合并权限），脚本无法设置，因此只设审查人。

---

## 迁移到新机器

1. 部署 `skills/`：一键脚本优先部署到 Hermes skills（`~/.hermes/skills/huawei-cloud/`），兼容 `~/.agents/skills/`
2. 配置 `~/.multica/config.json`（token + workspace）
3. 配置 `~/.bashrc` AK/SK、reporter 地址
4. 配置 SSH key 到 GitCode
5. 启动 `multica daemon`
6. agent/autopilot/workspace 在服务端，登录即同步

验证：
```bash
multica workspace list
bash -n ~/.agents/skills/skill-auto-test-pr/scripts/run-pipeline.sh
node ~/.agents/skills/huawei-cloud-skill-reporter/report.mjs <skill> success "migration-test"
```

---

## 智能体指令（creator 智能体服务端配置）

```
【任务受理流程（必须遵守顺序）】
1. 收到开发任务后，首先运行 feature 类型检查：
   bash ~/.agents/skills/huawei-cloud-skill-creator/scripts/feature-gate.sh <multica_issue_id>
   - 若脚本 exit 2（非 feature 类型 issue）：不要开发，在该 issue 上回复"该 GitCode issue 非 feature 类型，不进行开发"，并 multica issue status 置为 blocked，然后结束。绝不提交 PR。
   - 若脚本 exit 0 正常：继续。
2. 通过检查后，立即运行：
   bash ~/.agents/skills/skill-auto-test-pr/scripts/pre-create-hook.sh <multica_issue_id>
   作用：将 t_skills_dev_flow.node_dev_status 置为 processing（开发中），确保平台状态即时正确显示。
3. 然后按 huawei-cloud-skill-creator 六阶段流程开发 skill。

【目标仓库动态解析（必须遵守）】
在创建 PR 之前，先从当前 issue 的 description 中读取来源仓库字段，格式为：
  **来源仓库：** <owner>/<repo>
- 若该字段存在，PR 必须提交到该来源仓库：git@gitcode.com:<owner>/<repo>，分支 add-skill/<skill-name>，并关联对应 GitCode issue。
- 若该字段不存在或不是 huaweicloud-skills 仓库，回退到默认仓库：git@gitcode.com:developer-skill/huaweicloud-skills-dev
- 不要向任何其他仓库提交 PR。

【PR 审查人】
创建 PR 后，为 PR 设置审查人（assignee）：
  POST {GITCODE_API}/repos/<owner>/<repo>/pulls/<pr_number>/assignees
  body: {"assignees":"HugLovesWine"}
  （HugLovesWine 为默认审查人；若该用户无目标仓库权限则忽略并记录）

【工作要求】
写skill，测试skill，然后根据上面解析出的目标仓库提交 gitcode PR（Fixes #issue），创建后添加审查人，并更新 t_skills_dev_flow.node_dev_status=done。
```

---

## 已知边界

1. webhook 仅配置在旧仓库 `developer-skill/huaweicloud-skills-dev`
2. 合并人不可脚本设置（需网页 JWT + 合并权限）
3. 双 PR 路径依赖幂等兜底
4. tester Phase 4/6 真实环境测试需要华为云 AK/SK
