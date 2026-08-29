# GRP-DESIGN: skill-publish sync_skill_platform.sh 方案

> 基于 gitsync 对比分析后的最终落地方案。

## 一、背景

skill-publish 是三个 skill（creator/tester/publish）中唯一没有 scripts/ 目录的。
SRE agent 手动执行 GitHub/GitCode push 时存在两个实际问题：

1. **逐文件 commit**：hcs-obs-metaquery（9 文件）产生了 9 个 commit，发布仓库 commit 历史碎片化
2. **备份分支命名不统一**：有时 YYYYMMDDHHMMSS，有时 Unix timestamp

## 二、gitsync 对比结论

gitsync 是 bash 入口 + Python 工具链（25 模块 + 10 测试），做全仓库增量 git 镜像。
我们是单 skill 目录覆盖，不需要 cherry-pick/commit-tree/map/身份重写。
用 bash 脚本即可，不需要引入 Python 工具链。

参考 gitsync 的 2 点：
- 1 skill = 1 commit（gitsync 每次 skill 变更是 1 个 commit）
- 备份分支统一命名 backup/pre-sync-{YYYYMMDDHHMMSS}（gitsync 用 YYYYMMDDHHMMSS+微秒+pid，我们不需要微秒/pid，A2A 串行无并发）

### ClawHub 版本历史

ClawHub 平台本身支持版本历史查询：
- `GET /api/v1/skills/{slug}/versions` 返回 items 数组（version/createdAt/changelog）
- `GET /api/v1/skills/{slug}` 返回 latestVersion + stats.versions（版本总数）
- 版本历史由 ClawHub 平台维护，SRE 发布时 POST payload 带 version + changelog 字段，ClawHub 自动记录
- 不受本次改动影响——sync_skill_platform.sh 只管 GitHub/GitCode 的 git push，ClawHub 发布仍是独立的 curl API 调用

### ClawHub 个人模式/组织模式

ClawHub 的个人/组织模式逻辑在 SKILL.md 中已完整定义，不受新脚本影响：
- 个人发布：POST 不带 ownerHandle，URL 从 GET API 返回的 owner.handle 拼接（`https://clawhub.ai/{handle}/skills/{slug}`）
- 组织发布：POST 带 ownerHandle（$CLAWHUB_OWNER_HANDLE 非空时），URL 用 ownerHandle 拼接
- gitsync 的 native 模式总是带 ownerHandle（从 GET /api/v1/whoami 获取），我们不带（由 token 自动归属），两种方式都能工作

## 三、方案

### 新建 scripts/sync_skill_platform.sh

**职责**：clone 目标仓库 → 检测已有目录 → 建备份分支 → 发布（覆盖 skill 目录）/ 下架（删除 skill 目录）→ 1 次 commit → push → 输出结果

**用法**：
```bash
# 发布
bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh --action publish --platform gitcode --skill-name {name} --skill-dir {skill_dir} --version {version}
# 下架
bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh --action unpublish --platform gitcode --skill-name {name} --issue-num {issue_number}
```

**参数**：
- --action：publish（发布）或 unpublish（下架）
- --platform：gitcode / github
- --skill-name：skill 名称（如 hcs-obs-metaquery）
- --skill-dir：源 skill 目录路径（publish 时必填，unpublish 时不需要）
- --version：版本号（publish 时用于 commit message，unpublish 时不需要）
- --issue-num：Issue 编号（unpublish 时用于 commit message，可选）

**环境变量**：
- GITCODE_TOKEN / GITHUB_TOKEN：目标仓库 push 鉴权（已由 event_pipeline 注入）
- GITCODE_PUBLISH_REPO / GITHUB_PUBLISH_REPO：目标仓库 namespace/repo（已由 repo-credentials.json 注入）

**执行流程**：
1. 解析参数 + 校验环境变量
2. git clone 目标仓库到临时目录（--depth 1 浅克隆，失败回退完整克隆）
3. 获取默认分支（不硬编码 main/master）
4. 检测 skills/{name}/ 是否已存在
5. publish 且目录已存在 → 建备份分支 backup/pre-sync-{YYYYMMDDHHMMSS} 并推远端
   unpublish 且目录存在 → 建备份分支 backup/pre-delist-{YYYYMMDDHHMMSS} 并推远端
   目录不存在 → publish 直接复制；unpublish 报告「无需下架」（非失败，exit 0）
6. publish：删除旧 skills/{name}/ + 复制新 skill 目录 + 清理 __pycache__/*.pyc/.DS_Store
   unpublish：删除 skills/{name}/ 目录
7. git add -A + git commit（1 次 commit）：
   publish：`feat: publish skill {name} v{version}`
   unpublish：`unpublish: remove skill {name} (Issue #{issue_number})`
8. git push origin {default_branch}
9. 输出 PUSHED=1 / PUSHED=0 + BACKUP_BRANCH + TARGET_URL
10. 清理临时目录

**退出码**：0=成功；1=参数/环境错误；2=git 操作失败

### SKILL.md 改动

第 5 步（按平台发布）的 GitHub/GitCode 部分从文字描述改为调脚本：
```
- GitHub/GitCode: 调脚本封装 clone→备份→覆盖→1次commit→push：
  bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh --action publish --platform github --skill-name {name} --skill-dir {skill_dir} --version {version}
  bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh --action publish --platform gitcode --skill-name {name} --skill-dir {skill_dir} --version {version}
```

第 6 步（覆盖 push 前安全备份）合并进脚本，不再单独描述。

下架流程第 2 步（按平台下架）的 GitHub/GitCode 部分同样改为调脚本：
```
- GitHub/GitCode: 调脚本封装 clone→备份→删除→1次commit→push：
  bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh --action unpublish --platform github --skill-name {name} --issue-num {issue_number}
  bash {{SKILL_DIR}}/skill-publish/scripts/sync_skill_platform.sh --action unpublish --platform gitcode --skill-name {name} --issue-num {issue_number}
```

## 四、不做的

| 项 | 排除依据 |
|----|---------|
| Python 工具链 | 场景简单（目录覆盖），bash 足够；gitsync 的 Python 是因为 cherry-pick/commit-tree/map 复杂逻辑 |
| cherry-pick / commit-tree | 不需要保留 commit 历史，目录覆盖即可 |
| map 文件 / patch-id 增量检测 | git diff --cached --quiet 检查内容一致即可 |
| 身份重写 | 不需要 |
| 并发锁 | A2A 串行 |
| 测试文件 | bash 脚本不做单元测试（与 create_skill_pr.sh / merge_skill_pr.sh 一致） |
| 文件大小限制 | 9 skill 最大 92KB，skill-creator 已限 40MB |
| 发布黑名单 | A2A 全链路验证的 skill 无需排除 |
| .publish-state.json | GET API 0.3s 足够快 |
| 跳过文件规则 | .gitignore 已排除；脚本内置 find 清理 __pycache__/*.pyc |
| bump 3→10 | 无版本冲突历史 |

## 五、能力对比

| 维度 | 老方案（SKILL.md 文字描述，agent 手动执行） | 新方案（sync_skill_platform.sh 脚本） |
|------|---------------------------------------------|--------------------------------------|
| commit 粒度 | 逐文件 commit（9 文件 = 9 commit） | 1 skill = 1 commit |
| commit message 格式 | agent 自行决定 | 统一格式 |
| 备份分支命名 | 不统一（YYYYMMDDHHMMSS / Unix timestamp 混用） | 统一 backup/pre-sync-{YYYYMMDDHHMMSS} |
| 下架备份 | 有但命名同样不统一 | 统一 backup/pre-delist-{YYYYMMDDHHMMSS} |
| 默认分支检测 | agent 自行判断（可能硬编码 main） | 脚本 git symbolic-ref 自动获取 |
| 错误处理 | 无（agent 手动执行） | set -uo pipefail + 退出码 0/1/2 |
| 临时目录清理 | 靠 agent 记得 rm -rf | 脚本自动清理 |
| 架构一致性 | 唯一没有 scripts/ 的 skill | 与 creator/tester 一致 |
| ClawHub 个人/组织模式 | SKILL.md curl 命令控制（不变） | 不受影响（脚本不管 ClawHub） |
| ClawHub 版本历史 | ClawHub 平台 API 维护（不变） | 不受影响 |
