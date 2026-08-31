# fetch-pr-skill 部署指南（SETUP）

本仓库是 fetch-pr-skill 技能包。clone 到新环境后，按以下步骤配置即可直接运行。

## 1. 前置依赖

```bash
# Python 依赖
pip install pymysql esdk-obs-python \
    huaweicloudsdkcore huaweicloudsdkecs huaweicloudsdkiam huaweicloudsdkobs

# CLI 工具
# - hcloud (华为云 KooCLI)：https://developer.huaweicloud.com/develop/cn-cli
# - jq
# - opencode（或你用的 Agent CLI）
```

### 外部依赖（部署环境已有）

- 评测 skill 一键部署脚本：仓库内 `tools/skills_verify/deploy_skill.sh`（打包 + 导入 multica + 分配 agent）
- `~/.agents/skills/huawei-cloud-skill-audit`：安全审计 skill（评测安全维度依赖）。本仓库同目录下已包含
  （`tools/skills_verify/huawei-cloud-skill-audit/`），clone 后复制到 `~/.agents/skills/` 即可：
  ```bash
  cp -r tools/skills_verify/huawei-cloud-skill-audit ~/.agents/skills/
  ```

## 2. 配置文件（~/.fetch-pr-skill/）

clone 后需要在本机创建两个配置文件（**不含敏感值入库**，模板见 `config/`）：

```bash
mkdir -p ~/.fetch-pr-skill
cp config/obs-cred.conf.example ~/.fetch-pr-skill/obs-cred.conf
cp config/eval-db.conf.example  ~/.fetch-pr-skill/eval-db.conf
chmod 600 ~/.fetch-pr-skill/obs-cred.conf ~/.fetch-pr-skill/eval-db.conf
# 然后编辑两个文件填入真实值
```

| 文件 | 用途 |
|------|------|
| `~/.fetch-pr-skill/obs-cred.conf` | OBS 凭证（评测报告上传）+ `DEFAULT_REPO`（检索仓库） |
| `~/.fetch-pr-skill/eval-db.conf` | 评测数据库连接（hd_eval_* 写库） |

## 3. 环境变量

评测 agent 需要以下环境变量（敏感，不进仓库，注入 multica agent custom_env）：

| 变量 | 用途 |
|------|------|
| `GITCODE_TOKEN` | GitCode 公开 API（PRIVATE-TOKEN）：拉取 PR、合入、创建 ISSUE |
| `GITCODE_ACCESS_TOKEN` | GitCode 内部 API（Bearer JWT）：设置 ISSUE 负责人、PR 合并人（36 天自动刷新） |
| `GITCODE_REFRESH_TOKEN` | 刷新 access_token（24 天） |
| `GITCODE_PR_ASSIGNEE` | PR 审查人 login（v5 API，默认 `g30074593`） |
| `GITCODE_PR_ASSIGNEE_ID` | PR 合并人数字用户 id（内网 API，默认 `11027048`=g30074593） |
| `AUTO_MERGE_PR` | 自动合入开关（`on`/`off`，默认 off） |
| `HUAWEI_CLOUD_AK` | 华为云 AK（资源清理） |
| `HUAWEI_CLOUD_SK` | 华为云 SK（资源清理） |
| `HUAWEI_CLOUD_PROJECT_ID` | 华为云项目 ID（可用 IAM 自动解析） |
| `HUAWEI_CLOUD_REGION` | 区域（默认 cn-north-4） |
| `HUAWEI_CLOUD_SMN_TOPIC_URN` | 资源清理失败告警的 SMN 主题 |
| `HUAWEI_CLOUD_ACCESS_KEY` | 别名变量（部分 skill 测试脚本用，值同 `HUAWEI_CLOUD_AK`） |
| `HUAWEI_CLOUD_SECRET_KEY` | 别名变量（值同 `HUAWEI_CLOUD_SK`） |

> `GITCODE_ACCESS_TOKEN`/`GITCODE_REFRESH_TOKEN` 从浏览器登录 GitCode 后的 cookie 获取
> （`GITCODE_ACCESS_TOKEN`、`GITCODE_REFRESH_TOKEN`）。

## 4. 部署到 multica

使用评测 skill 一键部署脚本（`tools/skills_verify/deploy_skill.sh`）：

```bash
# 一键部署评测全套（fetch-pr-skill + huawei-cloud-skill-audit）
# 默认部署到 ~/.multica/config.json 的 workspace，分配给"评测机制报告"agent
bash tools/skills_verify/deploy_skill.sh

# 指定工作区（名称或 ID）
bash tools/skills_verify/deploy_skill.sh --workspace "SKILLS开发验证(测试)"

# 指定 agent + 更新 token
bash tools/skills_verify/deploy_skill.sh --agent 评测机制报告 --token mul_xxx

# 预览将执行的动作
bash tools/skills_verify/deploy_skill.sh --dry-run
```

脚本会自动：
1. 部署 `huawei-cloud-skill-audit` 到 `~/.agents/skills/`（本机安全审计依赖）
2. 打包 `fetch-pr-skill` 并导入 multica
3. 分配给目标 agent

## 5. 验证

```bash
# 单元测试（在 scripts/ 目录）
cd scripts && python3 -m pytest eval/tests/ -v

# 端到端：拉取并评测一个 PR
bash scripts/fetch_pr_skill.sh "https://gitcode.com/<owner>/<repo>/pull/<num>"
```

## 注意

- 两个配置文件和环境变量都是**敏感凭证**，切勿提交到 git 仓库
- `GITCODE_REFRESH_TOKEN` 约 24 天过期，过期后需从浏览器 cookie 重新获取并更新
- 评测仓库缓存在 `/tmp/fetch-pr-skill/`，重复评测不会重复 clone
