# Hermes 自动安装说明 (HERMES-INSTALL.md)

本文件是 skillsopr 工具包的**安装说明书**。生产环境 Hermes 读取本文件即可自动完成安装:
把 `tools/` 内所有 skill 与脚本部署到 **Hermes 技能目录** `~/.hermes/skills/<category>/`
(默认分类 `huawei-cloud`, 可用 `HERMES_SKILLS_CATEGORY` 覆盖), 同时兼容部署到
`~/.agents/skills/`(multica agent 环境, 设 `SKIP_AGENTS_SKILLS=1` 可跳过)。

## 一、安装目标

| 内容 | 目标路径 |
|---|---|
| 工具链 skills(12 个, 见下表) | `~/.hermes/skills/<category>/<skill-name>/` |
| 各 skill 配套脚本 | 随 skill 目录, 落在其 `scripts/` 子目录 |
| agents 兼容副本 | `~/.agents/skills/<skill-name>/`(可跳过) |
| 运行环境配置 | `~/.agents/skills/.env`(模板生成, 需填真实值) |

## 二、包内包含的 skills(12 个)

| skill | 来源目录 | 用途 |
|---|---|---|
| find-skills | `skills_dev/skills/find-skills` | 查找已安装 skill |
| huawei-cloud-find-skills | `skills_dev/skills/huawei-cloud-find-skills` | 华为云 skill 查找 |
| huawei-cloud-skill-creator | `skills_dev/skills/huawei-cloud-skill-creator` | Skill 创建器(六阶段开发) |
| huawei-cloud-skill-tester | `skills_dev/skills/huawei-cloud-skill-tester` | Skill 测试器(八阶段测试) |
| huawei-cloud-skill-reporter | `skills_dev/skills/huawei-cloud-skill-reporter` | Skill 使用遥测上报 |
| skill-auto-test-pr | `skills_dev/skills/skill-auto-test-pr` | 自动测试流水线 |
| huawei-cloud-skill-basictest | `skills_test/huawei-cloud-skill-basictest` | 专业功能测试(七阶段评测) |
| huawei-cloud-skill-tester | `skills_test/huawei-cloud-skill-tester` | 测试器(评测引擎) |
| fetch-pr-skill | `skills_verify/fetch-pr-skill` | PR 拉取 + multica 分配 |
| huawei-cloud-skill-audit | `skills_verify/huawei-cloud-skill-audit` | 评测机制报告 |
| gitcode-event-handler | `skills_gitcode/gitcode-event-handler` | GitCode webhook 事件处理 |
| gitsync-platform-sync | `skills_distribute/gitsync-platform-sync` | GitCode→GitHub/ClawHub/DB 同步 |

## 三、安装步骤(Hermes agent 按此执行)

```bash
# 0. 解包(示例; 实际文件名以 OBS release/ 下最新为准)
tar xzf skillsopr-tools-*.tar.gz -C /tmp && cd /tmp/skillsopr

# 1. 部署 12 个 skill 到 Hermes 技能目录(按分类子目录)
#    ⚠️ huawei-cloud-skill-tester 在 skills_dev/ 与 skills_test/ 各有一份:
#    skills_dev 版(797 行, 完整工具链)最后安装, 确保覆盖 skills_test 版(552 行)。
HERMES_CAT="${HERMES_SKILLS_CATEGORY:-huawei-cloud}"
mkdir -p ~/.hermes/skills/$HERMES_CAT
# 1a. 先装其余 11 个(排除 skills_dev 下的 tester, 避免过早被覆盖)
for d in $(find tools -name SKILL.md -printf '%h\n' | grep -v '^tools/skills_dev/skills/huawei-cloud-skill-tester$'); do
  name=$(basename "$d")
  cp -r "$d" ~/.hermes/skills/$HERMES_CAT/$name
  echo "  skill -> $HERMES_CAT/$name"
done
# 1b. 最后装 skills_dev 完整版 tester(覆盖 skills_test 版)
cp -r tools/skills_dev/skills/huawei-cloud-skill-tester ~/.hermes/skills/$HERMES_CAT/
echo "  skill -> $HERMES_CAT/huawei-cloud-skill-tester (skills_dev 完整版)"

# 2. agents 环境兼容副本(可选; SKIP_AGENTS_SKILLS=1 跳过)
if [ "${SKIP_AGENTS_SKILLS:-0}" != "1" ]; then
  mkdir -p ~/.agents/skills
  for d in $(find tools -name SKILL.md -printf '%h\n' | grep -v '^tools/skills_dev/skills/huawei-cloud-skill-tester$'); do
    name=$(basename "$d")
    cp -r "$d" ~/.agents/skills/$name
  done
  cp -r tools/skills_dev/skills/huawei-cloud-skill-tester ~/.agents/skills/
fi

# 3. 脚本可执行权限
find ~/.hermes/skills/$HERMES_CAT -name '*.sh' -exec chmod +x {} +

# 4. 运行环境配置模板(复制后填真实值, 禁止留空 export)
if [ ! -f ~/.agents/skills/.env ]; then
  cp tools/skills_dev/scripts/env.example ~/.agents/skills/.env 2>/dev/null \
    || touch ~/.agents/skills/.env
  chmod 600 ~/.agents/skills/.env
fi
```

## 四、逐工具集说明

- **skills_dev**(开发工具链): 也可直接跑一键部署
  `bash tools/skills_dev/deploy-skills-dev.sh [--repo <url>]` —
  自动完成 6 个 skill 双目标部署 + multica profile 配置 + token 验证。
- **skills_test**(测试能力): `tools/skills_test/deploy/deploy-skills-test.sh`
  部署 basictest+tester 到 multica 工作区; 纯本地安装按第三节手动复制即可。
- **skills_verify**(评测): `tools/skills_verify/deploy_skill.sh`
  部署 fetch-pr-skill + audit 到工作区/agent。
- **skills_gitcode**(webhook): `gitcode-event-handler` 需配合 Hermes Gateway
  路由 `/webhooks/gitcode-event`, 见其 SKILL.md 与 `hermes-webhook-debug` 技能。
- **skills_distribute**(分发): `gitsync-platform-sync` 含完整 Python 工具链,
  使用前 `cp templates/config.yaml.example config.yaml` 并填真实仓库/DB 值;
  依赖 PyYAML(必需)与 pymysql(测试用), 见其 SKILL.md。

## 五、安装后验证

```bash
# 1. 12 个 skill 全部可见(含 frontmatter)
for d in ~/.hermes/skills/$HERMES_CAT/*/; do
  [ -f "$d/SKILL.md" ] && head -1 "$d/SKILL.md" | grep -q '^---' \
    && echo "OK  $(basename $d)" || echo "BAD $(basename $d)"
done
# 2. 脚本语法
find ~/.hermes/skills/$HERMES_CAT -name '*.sh' -exec bash -n {} \; && echo "ALL SH OK"
# 3. gitsync 工具链自检(可选)
cd ~/.hermes/skills/$HERMES_CAT/gitsync-platform-sync \
  && PYTHONPATH=src python3 -m gitsync.cli --help >/dev/null && echo "gitsync OK"
```

## 六、安全约定

- 包内**不含任何真实凭据**(DB 密码 / GitCode token / 华为云 AK-SK 均为
  `REPLACE_WITH_*` 占位符); `.env` 由安装方填写真实值, 权限 0600。
- 生产 Hermes 安装后, 本 MD 可删除(安装动作已完成)。
