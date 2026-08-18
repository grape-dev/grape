# skill-publish 技能 — 设计 v3.0（定稿，单技能模型）

状态: 方案已确认，待实施
特性分支: feat/skill-sync-multiplatform
前置调研: docs/clawhub-publish-research.md

## 1. 需求与约束

### 1.1 需求
- 开发者完成 skill 创建后，可要求同步到 ClawHub 等平台（发布/下架）
- 两个入口可用：对话模式（已通 skill_sync_scene）+ Issue 评论模式（SRE 有正确技能）

### 1.2 核心决策（用户拍板）
- **skill 保持单选**（一个 agent 一个技能，不改成多选）
- **skill-publish 独立技能（切换用）**：与 deploy-guard/build-debug 并列，配置页下拉框可切换
- **不改 pipeline / webhook / A2A / 配置页单选模型 / worker 协议**——保持现有架构

## 2. 方案（全新增，零修改现有逻辑）

### 2.1 新建 skills/skill-publish/SKILL.md

SRE 的 skill 发布技能（进技能池，配置页可切换选用）：

```markdown
---
name: skill-publish
version: 1.0.0
description: |
  将 grape 生成的 skill 发布/下架到外部平台（ClawHub 等）。
  Triggers: 同步 skill, 发布 skill, 下架 skill, ClawHub, 撤回发布
tools: [curl, git]
---

# Skill 发布管理

## 安全硬约束
1. 令牌只用环境变量注入的 $CLAWHUB_TOKEN，严禁从数据库/配置文件/日志/系统目录寻找
2. 严禁翻系统目录考古（/usr/local/lib/hermes-agent 等）
3. 403/无权限 → 立即停止并告知用户，不绕行
4. token 不写入任何文件/日志/评论

## 入口判定
- 用户要求「同步/发布/下架 skill 到 ClawHub 等平台」时使用本技能
- 非发布类请求（部署 ECS 等）→ 用 cloud-deploy 技能

## 发布流程（ClawHub）
1. 定位 skill：仓库内 skills/{name}/ 或根目录 {name}/（含 SKILL.md）
2. 读取令牌：$CLAWHUB_TOKEN（环境变量注入，缺失 → 报告「未配置令牌，请到设置页配置」）
3. 校验本地内容：SKILL.md frontmatter（name/description/tags）+ 文件清单
4. 发布：
   curl -X POST https://clawhub.ai/api/v1/skills \
     -H "Authorization: Bearer $CLAWHUB_TOKEN" \
     -F payload='{"displayName":"...","slug":"...","version":"...","contentType":"...","tags":[...],"acceptLicenseTerms":true,"changelog":"..."}' \
     -F files=@SKILL.md ...
5. 校验：SHA256 与仓库逐字节比对 + ClawHub 搜索 API 确认可见 + 安全审计状态
6. 回复：成功（链接 + 安装命令）/ 失败（原因）

## 下架流程（ClawHub）
1. 读取令牌（同上）
2. DELETE https://clawhub.ai/api/v1/skills/{slug}（软删除，30 天 slug 保留）
3. 校验：搜索 API 确认已下架
4. 回复结果

## 版本策略
- 版本号取 SKILL.md frontmatter version（默认 0.1.0）
- 重复发布同 slug → ClawHub 返回冲突 → 报告用户（或按 update 流程发新版本）
```

### 2.2 skill 表登记（进技能池，配置页可切换）

- `configs/skill-description.json` 加 seed：
  ```json
  "skill-publish": {
    "name": "技能发布",
    "agent_type": "sre",
    "path": "skill-publish",
    "description": "skill 发布管理：将 grape 生成的 skill 发布/下架到 ClawHub 等外部平台"
  }
  ```
- db_setup 幂等补种 → skill 表出现 (skill-publish, sre, official)
- **效果**：配置页 SRE 下拉框出现「技能发布」选项——用户切换选用（同 deploy-guard/build-debug 待遇）

### 2.3 服务端令牌注入（env_extra）

- `src/api/event_pipeline.py` 新增 `_inject_platform_tokens(env_extra, repo_key)`
  （**新增函数**，不动现有 `_inject_cloud_credentials`）
- 逻辑：按仓库 owner（repo_owner）从 user_platform_tokens 读 clawhub_token
  → `env_extra["CLAWHUB_TOKEN"] = token`（无则空串）
- 注入点：任务 enqueue 前（与 _inject_cloud_credentials 并列调用）
- 效果：SRE 执行 skill-publish 时读得到令牌（缺失时技能内报「未配置」而非考古）

## 3. 使用场景（单技能切换）

| 场景 | SRE 配置页技能 | 行为 |
|---|---|---|
| 部署 ECS | cloud-deploy（默认） | 现状不变 |
| 同步 skill 到 ClawHub | 切换到 skill-publish | SRE 读 skill-publish → 发布/下架（不考古） |
| 对话入口同步 | 任意（skill_sync_scene 独立） | 已通，不受影响 |

**注意**：单技能模型下，配置页切换 skill-publish 后，该仓库 SRE 的部署任务（走 cloud-deploy 技能）会受影响——切换是「全局替换」不是「追加」。用户按需切换。

## 4. 改动清单（3 项，全新增）

| # | 文件 | 改动 | 类型 |
|---|---|---|---|
| 1 | skills/skill-publish/SKILL.md | 新建（发布/下架流程） | 新增 |
| 2 | configs/skill-description.json | skill-publish seed | 新增 |
| 3 | src/api/event_pipeline.py | `_inject_platform_tokens` 新增函数 | 新增 |

## 5. 不改动清单（硬约束）

| 组件 | 状态 |
|---|---|
| 配置页单选模型（agents.js/agents.py/DB） | ❌ 不改 |
| pipeline 路由/派发逻辑 | ❌ 不改 |
| webhook / A2A | ❌ 不改 |
| worker agent.py / 安装包 / 协议 | ❌ 不改 |
| OBS 分发 | ❌ 不改 |
| 对话 skill_sync_scene | ❌ 不改 |

## 6. 验证方案

- 单测：skill 表 seed 出现 skill-publish；_inject_platform_tokens 按 owner 注入（有/无令牌两场景）
- 集成：真实 Issue 评论「同步到 ClawHub」→ SRE（配置为 skill-publish）读技能执行，不考古
- 回归：cloud-deploy 部署任务不受影响

## 7. 待确认

- [ ] skill-publish 的 skill_id 解析：配置页切换时 _find_skill 能否找到（seed 后即可，预计无问题）
- [ ] CLAWHUB_TOKEN 注入仅对 sre 任务，还是所有任务（建议仅 sre/需要时）
- [ ] GitHub/Gitee 平台（后续在技能内加章节）
