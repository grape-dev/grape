# skills_dev 工具链迁移指南(MIGRATION)

目标:把 skills_dev 工具链从当前机器迁移到新机器,消除写死值,保证新机器开箱可跑。

> 原则:一切机器相关/凭据/仓库归属的值都只存在于 `~/.agents/skills/.env`;
> 脚本内仅保留 `占位符默认值`(迁移后不包含任何真实凭据,可安全入库)。

---

## 0. 迁移总览

| 类别 | 修改方式 | 涉及文件 |
|------|----------|----------|
| 凭据(DB 密码 / GitCode token) | 删默认值,只读 .env,缺失即报错退出 | run-pipeline.sh / feature-gate.sh / report-dev-*.sh / pre-create-hook.sh |
| 仓库/owner/PR 指派人 | 默认值改占位符,由 .env 注入 | run-pipeline.sh(顶层 + skill-auto-test-pr 副本) |
| URL(multica / gitcode / 索引仓库) | 默认值改占位符或由 env 拼装 | report-dev-*.sh / run-pipeline.sh / search-skills.py |
| 机器路径(multica 二进制) | `command -v` 探测 + PATH 兜底 | run-pipeline.sh / deploy-skills-dev.sh |
| 工作区/小队 UUID | 由 .env(MULTICA_WORKSPACE_ID)与部署参数提供,文档替换 | SKILL.md / agent_dev_instructions.md |

---

## 1. 凭据:删除脚本内真实默认值(必改,共 6 个文件)

当前现状(真实值明文):

```bash
# run-pipeline.sh:12-16 / feature-gate.sh:28,31 / report-dev-*.sh / pre-create-hook.sh
[ -z "${DB_HOST:-}" ] && DB_HOST="192.168.1.18"
[ -z "${DB_USER:-}" ] && DB_USER="root"
[ -z "${DB_PASS:-}" ] && DB_PASS="<REAL_DB_PASS>"       # ← 旧代码里的真实密码(已删除)
[ -z "${GITCODE_TOKEN:-}" ] && GITCODE_TOKEN="<REAL_TOKEN>" # ← 旧代码里的真实 token(已删除)
```

改为(统一模式):

```bash
# 只读 .env,缺失即报错退出,不留默认值
[ -z "${DB_PASS:-}" ] && { echo "[ERROR] DB_PASS 未设置,请在 ~/.agents/skills/.env 配置" >&2; exit 1; }
[ -z "${GITCODE_TOKEN:-}" ] && { echo "[ERROR] GITCODE_TOKEN 未设置,请在 ~/.agents/skills/.env 配置" >&2; exit 1; }
```

需要修改的文件与行(以当前 master 为准):
- scripts/run-pipeline.sh         :12-16(DB 四项), :31(GITCODE_TOKEN)
- scripts/feature-gate.sh         :28(DB_PASS), :31(GITCODE_TOKEN)
- scripts/report-dev-done.sh      :18, :20
- scripts/report-dev-failed.sh    :18, :20
- scripts/report-dev-processing.sh:21, :23
- scripts/pre-create-hook.sh      :22, :24

> ⚠️ 迁移前先轮换:当前 GITCODE_TOKEN 已存在于 git 历史,视为泄露,换新 token 后
> 只写入新机器 .env,不再入库。

---

## 2. 仓库归属:默认值改占位符(2 个文件)

```bash
# run-pipeline.sh:29-37(skill-auto-test-pr/scripts/run-pipeline.sh:15-19 同款)
GITCODE_REPO_SSH="${GITCODE_REPO_SSH:-git@gitcode.com:developer-skill/huaweicloud-skills-dev}"
GITCODE_OWNER="${GITCODE_OWNER:-developer-skill}"
GITCODE_REPO="${GITCODE_REPO:-huaweicloud-skills-dev}"
GITCODE_CHECKOUT="${GITCODE_CHECKOUT:-huaweicloud-skills-dev}"
GITCODE_PR_ASSIGNEE="${GITCODE_PR_ASSIGNEE:-HugLovesWine}"
```

改为(占位符,迁移时在 .env 填新仓库):

```bash
GITCODE_REPO_SSH="${GITCODE_REPO_SSH:-git@gitcode.com:OWNER/REPO}"
GITCODE_OWNER="${GITCODE_OWNER:-OWNER}"
GITCODE_REPO="${GITCODE_REPO:-REPO}"
GITCODE_CHECKOUT="${GITCODE_CHECKOUT:-REPO}"   # 本地 checkout 目录名,与 .env 保持一致
GITCODE_PR_ASSIGNEE="${GITCODE_PR_ASSIGNEE:-}"
```

> ⚠️ GITCODE_CHECKOUT 语义是"本地 skills 仓库 checkout 目录名",
> 与 env.example:14 的 `main`(分支名)含义不同,迁移时统一在 .env 里配成目录名。

新机器 .env 填写示例:

```bash
export GITCODE_OWNER="developer-skill"
export GITCODE_REPO="huaweicloud-skills-dev"
export GITCODE_CHECKOUT="huaweicloud-skills-dev"
export GITCODE_PR_ASSIGNEE="<新指派人>"
```

---

## 3. URL:默认值改占位符或 env 拼装(4 处)

### 3.1 MULTICA_BASE_URL(report-dev-*.sh ×3)

```bash
# 现状:report-dev-done.sh:27 / report-dev-failed.sh:28 / report-dev-processing.sh:25
MULTICA_BASE_URL="https://multica.clouddeveloper.club/skill-test/issues"
```

改为用 env 拼装(工作区 slug 由 MULTICA_WORKSPACE_ID 对应):

```bash
MULTICA_SERVER_URL="${MULTICA_SERVER_URL:-https://multica.clouddeveloper.club}"
MULTICA_WORKSPACE_SLUG="${MULTICA_WORKSPACE_SLUG:-skill-test}"
MULTICA_BASE_URL="${MULTICA_SERVER_URL}/${MULTICA_WORKSPACE_SLUG}/issues"
```

### 3.2 GITCODE_API(run-pipeline.sh:30)

```bash
GITCODE_API="${GITCODE_API:-https://api.gitcode.com/api/v5}"   # 保留即可,公共端点
```

### 3.3 索引仓库(search-skills.py:11-12)

```python
# 现状:写死 2501_91318609/skills-for-index + ref=main
DEFAULT_INDEX_URL = "https://gitcode.com/api/v5/repos/2501_91318609/skills-for-index/contents/skills-index/index.json?ref=main"
```

改为环境变量 + 占位:

```python
SKILLS_INDEX_REPO = os.environ.get("SKILLS_INDEX_REPO", "OWNER/skills-for-index")
DEFAULT_INDEX_URL = f"https://gitcode.com/api/v5/repos/{SKILLS_INDEX_REPO}/contents/skills-index/index.json?ref=main"
```

### 3.4 上游技能仓库(find-skills/SKILL.md:124,150,223)

文档常量,迁移时全局替换 `huaweicloud/huaweicloud-skills` 与分支 `master`。

---

## 4. 机器路径:探测而非写死(2 个文件)

### 4.1 multica 二进制(run-pipeline.sh:18)

```bash
# 现状
MULTICA="${MULTICA:-/home/gaoyunjiao/multica}"
# 改为
MULTICA="${MULTICA:-$(command -v multica || echo /usr/local/bin/multica)}"
```

### 4.2 部署脚本探测列表(deploy-skills-dev.sh:79)

```bash
# 现状:写死 3 个绝对路径
for p in /home/gaoyunjiao/multica /usr/local/bin/multica /opt/multica/multica; do
# 改为:先 command -v,再兜底列表
for p in "$(command -v multica 2>/dev/null)" /usr/local/bin/multica /opt/multica/multica; do
```

---

## 5. 工作区/小队 UUID:环境变量与部署参数(3 个文件)

- `deploy/agent_dev_instructions.md:88` 的 `mention://squad/c05558fa-...`
  → 文档里改为 `mention://squad/${SQUAD_TEST_ID}` 占位,部署时用 `multica squad list`
    查新工作区实际 ID 替换。
- `skills/skill-auto-test-pr/SKILL.md:40-41` 的 agent/autopilot ID
  → 同样改为 `${AGENT_ID}` / `${AUTOPILOT_ID}` 占位。
- 顺手修正:SKILL.md 正文 90/93 行的 `98ff4acd` 与 40 行 `98ffa4cd` 拼写不一致,
  迁移时统一。

---

## 6. 新机器部署步骤(照此执行)

```bash
# 1. 依赖安装(按需,清华镜像提速)
sudo apt install -y python3 nodejs jq git curl
pip3 install pymysql -i https://pypi.tuna.tsinghua.edu.cn/simple
# multica CLI(二进制 scp 过去放 /usr/local/bin/)
# hcloud KooCLI:参考华为云官方安装
# hermes:按官方文档安装

# 2. 克隆仓库
git clone git@gitcode.com:developer-skill/skillsopr.git
# 或 scp 整个目录(含工具链)
scp -r ~/skillsopr/tools/skills_dev newhost:/opt/skills_dev/

# 3. 生成 .env 并填写真实值(不填任何写死值)
cp scripts/env.example ~/.agents/skills/.env
vim ~/.agents/skills/.env
# 必填:GITCODE_TOKEN / DB_PASS / DB_HOST(内网可达) / HUAWEICLOUD_SDK_AK|SK
# 可选:MULTICA_TOKEN / MULTICA_WORKSPACE_ID / GITCODE_OWNER / GITCODE_REPO ...

# 4. GitCode SSH key
ssh-keygen -t ed25519
cat ~/.ssh/id_ed25519.pub   # 添加到 GitCode 设置 → SSH 公钥

# 5. hcloud CLI 登录(华为云 AK/SK)
hcloud configure set --cli-mode=AKSK

# 6. 部署工具链(安装 skills 到 ~/.agents/skills + hermes/opencode 插件)
bash deploy-skills-dev.sh --profile dev

# 7. 验证
bash scripts/run-pipeline.sh --check      # 若有自检入口
multica --profile dev workspace list
hermes skills list | grep huawei-cloud
```

---

## 7. 迁移后验收清单

- [ ] grep -rn 'SkillsMysql\|ZKJa8' 脚本目录 → 无命中(凭据已清干净)
- [ ] .env 里 DB 四项、GITCODE_TOKEN、AK/SK 均已填真实值且非空
- [ ] `bash -n` 所有 .sh 语法通过
- [ ] multica 可连新工作区(`multica workspace list` 有输出)
- [ ] MySQL 可达:新机器能连 DB_HOST 3306(内网机器记得开白名单)
- [ ] 一次最小演练:手工跑一个 dev 任务,确认 flow 节点状态能写回
