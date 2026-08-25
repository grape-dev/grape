# skill 发布统一方案 — 设计 v5.2（定稿）

状态: v4.5 发布配置仓库级已实施验证通过（真实撤回 ClawHub 成功）；v5.2 创建→分发闭环（skill-creator + skill-tester + 转交规则）——已实施待验证
特性分支: feat/skill-sync-multiplatform
前置: docs/clawhub-publish-research.md / skill-publish-design.md(v3.0~v4.5)

## 1. 核心决策（用户全部拍板）

| # | 决策项 | 决定 |
|---|---|---|
| 1 | skill 配置模型 | 保持单选（一个 agent 一个技能，不改多选） |
| 2 | skill-publish 形态 | 独立技能（切换用），进技能池，配置页可切换 |
| 3 | skill_sync_scene | 废弃（A1 彻底：scene + sync_service + bind 表 + upsert_bind 全删） |
| 4 | 同步请求统一链路 | 对话/Issue 入口 → SRE → skill-publish 技能执行 |
| 5 | 对话回传 | 引导复用原 Issue（#N 评论「请同步」），结果回评 Issue；对话回复「已交办，见 Issue #N」 |
| 6 | 已发布 skill 遗留 | skill-publish 技能接管（下架流程支持，靠技能定位仓库目录） |
| 7 | 配置页切换影响 | 接受（用户按需切换，部署时切回 cloud-deploy） |
| 8 | 发布范围 | **ClawHub + GitHub + GitCode**（三平台） |
| 9 | 确认策略 | **默认二次确认**（评论「确认发布…回复『确认』执行」）；用户明确要求直接发布时可跳过 |
| 10 | 幂等 | **自动更新版本重新发布**（同 slug 存在 → 版本递增） |
| 11 | 令牌存储 | **方案 B（v4.3 实施）**：repo_platform_tokens（repo_key + platform + token）——仓库级——加平台=加行 |
| 12 | 令牌注入 | 仅 sre 任务、**按仓库读**（repo_key → repo_platform_tokens）——谁评论触发都一样——不再按评论人/owner 读用户表 |
| 13 | **目标仓库（v4.2→v4.4）** | **仓库级统一存储**：repo_platform_tokens.publish_repo（按 repo_key + platform）——发布读仓库配置 push——未配置报告跳过；评论指定仓库（「到 owner/repo」）覆盖默认 |
| 14 | **配置入口（v4.3）** | General Settings → Credentials（ClawHub/GitHub/GitCode 区块）——保存带当前仓库（repo_key）——按仓库写入 |

## 1.1 repo_platform_tokens 表结构（v4.5——按评审优化）

```
CREATE TABLE repo_platform_tokens (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    repo_key      VARCHAR(256)  NOT NULL COMMENT '仓库唯一键 gitcode:signway/fluxgate（系统统一概念——非 skill 编码）',
    platform      VARCHAR(32)   NOT NULL COMMENT '平台: github/gitcode/clawhub（VARCHAR 而非 ENUM——加平台不 DDL）',
    token         VARCHAR(2048) NULL COMMENT '【警告：生产必须加密，禁止明文泄露！】平台访问token（当前明文过渡）',
    publish_repo  VARCHAR(512)  NULL COMMENT '发布目标仓库，统一格式：namespace/repo',
    created_at    DATETIME      DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新自动刷新（评审指出——原设计不会自动更新）',
    is_deleted    TINYINT       DEFAULT 0 COMMENT '软删除 0未删除 1已删除（敏感凭证不物理删——保审计）',
    PRIMARY KEY (id),
    UNIQUE KEY uk_repo_platform (repo_key, platform, is_deleted),
    KEY idx_platform (platform)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Skill发布平台凭证表';
```

> 评审采纳点：自增 id / created_at / ON UPDATE 自动刷新 / 软删除 is_deleted /
> platform VARCHAR(32) / NULL 表示未配置（不用空串）/ 唯一索引带 is_deleted（软删后可重建同 repo+platform）。
> 保留 repo_key 命名（系统已有统一概念 managed_repos/devops_agent_session 都用 repo_key——
> 语义=仓库——发布配置按仓库（项目维度））。
> token 明文过渡（评审第四节折中）——注释警告——生产必须加密。

## 1.2 skill 创建 → 分发闭环（v5.2——已实施）

```
Issue 需求 → skill-creator（SRE——查重→澄清确认→CLI/SDK/
  OpenAPI 三级调研→骨架生成→提交用户代码仓→清理→A2A）
→ skill-tester（qa agent 执行——功能验证+规范脚本化+
  gitleaks 密钥扫描——门控报告）
→ skill-publish（SRE——三平台发布——v4.5 增强幂等）
→ 回评 Issue

关键决策：
· 生成物进【用户代码仓】（用户资产——版本管理）——不进
  系统技能池（技能池保持官方纯净——发布/测试从仓库读）
· skill-tester 由 qa agent 类型执行（复用——qa 技能本身
  不动——独立 skill-tester 技能）
· 触发不改链路：Issue 照旧进 issue agent——识别创建/发布
  需求 → A2A 转交 sre（issue-advance 指令加转交规则）
· 借鉴 skillsopr：creator 六阶段（查重/确认门控/三级调研）
  + tester（basictest 功能验证/spec_check 脚本化/gitleaks）
· 场景（v5.2 补充）：【创建链路前提】——Dashboard 选
  「Skill 开发」（skill-dev 场景——agent-skill-mapping.json
  已修正 mapping：coding→skill-creator / qa→skill-tester /
  sre→skill-publish）——A2A 转交不带技能——coding 用会话
  默认技能——切场景后默认=skill-creator——创建链路才通
  （切回 default 恢复 implementation）
```

## 2. 改动清单

### 新增（3 项）

| # | 文件 | 改动 |
|---|---|---|
| 1 | skills/skill-publish/SKILL.md | 新建：三平台发布/下架流程 + 安全约束（见 §3） |
| 2 | configs/skill-description.json | skill-publish seed（agent_type=sre, official） |
| 3 | src/api/event_pipeline.py | `_inject_platform_tokens` 新增（sre 任务、按执行用户注入 CLAWHUB_TOKEN + 平台令牌） |

### 删除（4 项）
| # | 文件 | 改动 |
|---|---|---|
| 4 | src/im/skill_sync_scene.py | 删除 |
| 5 | src/plugins/sync_service.py | 删除 |
| 6 | src/api/db_setup.py | 移除 skill_sync_bind 建表语句（旧表部署时手动 DROP） |
| 7 | src/user/db.py | 移除 upsert_bind 调用（960 行附近） |

### 改造（2 项）
| # | 文件 | 改动 |
|---|---|---|
| 8 | src/im/handler_async.py | 移除 scene 调用（_run_sync_scene 430 行 + match_scene 544-551 行） |
| 9 | skills/chat/SKILL.md | 加指引：「同步/发布/下架 skill 请求 → 复用原 Issue 评论『请同步…』，走流水线由 SRE 执行」 |

## 3. skill-publish 技能内容（三平台）

```markdown
---
name: skill-publish
version: 1.0.0
description: |
  将 grape 生成的 skill 发布/下架到外部平台（ClawHub/GitHub/GitCode）。
  Triggers: 同步 skill, 发布 skill, 下架 skill, ClawHub, GitHub, GitCode, 撤回发布
tools: [curl, git]
---

## 2.1 v5.2 改动清单（创建→分发闭环——已实施）

| # | 文件 | 改动 |
|---|---|---|
| 1 | skills/skill-creator/SKILL.md（新） | skill 创建生成器：查重→澄清确认→三级调研→骨架→提交用户仓库→清理→A2A（v5.2 优化：环境说明段防侦察/调研禁猜+时间盒+失败门控——对齐 skillsopr） |
| 2 | skills/skill-tester/SKILL.md（新） | skill 功能验证：跑 skill + 规范脚本化 + gitleaks（qa 执行） |
| 3 | skills/skill-tester/scripts/spec_check.py（新） | 规范校验脚本（frontmatter/结构/可执行——不靠 LLM 判断） |
| 4 | configs/skill-description.json | +skill-creator（coding）/ +skill-tester（qa）seed |
| 5 | skills/issue-advance/SKILL.md | +2 行转交规则（创建→coding / 发布→sre） |
| 6 | configs/agent-skill-mapping.json | skill-dev 场景 mapping 修正（coding→skill-creator/qa→skill-tester/sre→skill-publish） |
| 7 | skills/skill-publish/SKILL.md | 发布增强（幂等探测/hash/bump 进位/备份分支/发布记录/失败不中断） |
| 8 | docs/skill-publish-design.md | v5.2 设计（本文件） |

# Skill 发布管理

## 安全硬约束
1. 令牌只用环境变量注入的 $CLAWHUB_TOKEN / $GITHUB_TOKEN / $GITCODE_TOKEN
2. 严禁从数据库/配置文件/日志/系统目录寻找 token
3. 严禁翻系统目录考古（/usr/local/lib/hermes-agent 等）
4. 403/无权限 → 立即停止并告知用户
5. token 不写入任何文件/日志/评论

## 入口判定
- 同步/发布/下架 skill 请求 → 本技能
- 部署类请求（ECS 等）→ 告知用户需切换 cloud-deploy 技能

## 确认策略
- 默认：评论「确认发布【{skill} v{version}】到 {平台列表}？回复『确认』执行」
- 用户明确「直接发布/不用确认」→ 跳过确认直接执行

## 定位 skill
- 仓库内 skills/{name}/ 或根目录 {name}/（含 SKILL.md）
- version 取 SKILL.md frontmatter（默认 0.1.0）；slug = name 转小写连字符

## 发布流程
1. 定位 skill + 读 frontmatter
2. 读令牌（缺失平台 → 报告「未配置 {平台} 令牌，请到设置页配置」，跳过该平台）
3. 按平台发布:
   - ClawHub: POST https://clawhub.ai/api/v1/skills (multipart payload+files, Bearer $CLAWHUB_TOKEN)
   - GitHub: push 到【用户配置的目标仓库】（skills/{name}/ 目录）
   - GitCode: push 到【用户配置的目标仓库】（skills/{name}/ 目录）
4. 幂等: 同 slug 已存在 → 版本递增重新发布（ClawHub: version bump；GitHub/GitCode: 覆盖 push）
5. 校验: SHA256 逐字节比对 + 平台搜索 API 确认 + 安全审计（ClawHub）
6. 回复: 各平台成功（链接 + 安装命令）/ 失败（原因）
- 目标仓库（GitHub/GitCode）: 读用户配置（未配置 → 报告「未配置发布目标仓库，请到设置页配置」——跳过该平台）

## 下架流程
1. 读令牌
2. 按平台下架:
   - ClawHub: DELETE /api/v1/skills/{slug}（软删除 30 天）
   - GitHub/GitCode: 删除目标仓库 skills/{name}/ 目录 push
3. 校验 + 回复
```

## 4. 令牌存储与注入

### 4.1 存储（方案 B：platform 区分 + 通用 token 列）

`user_platform_tokens` 表改造：

```sql
-- 现状（clawhub 专用列）→ 改为通用 token 列
ALTER TABLE user_platform_tokens RENAME COLUMN clawhub_token TO token;
```

```
┌─────────┬──────────┬─────────────┬─────────────┐
│ user_id │ platform │ token       │ updated_at  │
├─────────┼──────────┼─────────────┼─────────────┤
│ admin   │ clawhub  │ clh_xxx...  │ ...         │
│ admin   │ github   │ ghp_xxx...  │ ...         │
│ admin   │ gitcode  │ xxx...      │ ...         │
│ xiaowu  │ clawhub  │ clh_yyy...  │ ...         │
└─────────┴──────────┴─────────────┴─────────────┘
```

- `platform` 列值扩展：clawhub / github / gitcode（主键 (user_id, platform) 天然支持）
- `token` 通用列替代 `clawhub_token`
- **加平台 = 加一行数据，不改表结构**（符合「不写死清单」原则）
- 设置页 Credentials：ClawHub 区块（token）；GitHub 区块（token + **发布目标仓库**）；GitCode 区块（**发布目标仓库**——token 复用 OAuth 登录的 gitcode_api_token（user_accounts，自动填充无需手动配））

### 4.2 注入（v4.6 当前实现——仓库级）

- `_inject_platform_tokens(env_extra, repo_key)` — 仅 sre 任务，按仓库（repo_key）从 repo_platform_tokens 读：
  - CLAWHUB_TOKEN / GITHUB_TOKEN / GITCODE_TOKEN ← repo_platform_tokens.token（仓库级，不依赖发起账号）
  - GITCODE_PUBLISH_REPO / GITHUB_PUBLISH_REPO ← repo_platform_tokens.publish_repo（发布目标仓库）
  - CLAWHUB_OWNER_HANDLE ← repo_platform_tokens.owner_handle（v4.6 新增列——ClawHub 组织发布者；空=个人发布）
  - 老库未迁移 owner_handle 列时自动降级（不带该列查询）
- 缺失平台令牌 → 技能报告「未配置」并跳过该平台（不阻塞其他平台）

### 4.3 ClawHub ownerHandle 契约（v4.6）

- **存储**：repo_platform_tokens.owner_handle（VARCHAR 128，仓库级，NULL/空=个人账号发布）
- **配置入口**：设置页 ClawHub 区块「Owner Handle」输入框（PUT /settings/credentials body.clawhub_owner_handle）
- **注入**：CLAWHUB_OWNER_HANDLE 环境变量（sre 任务）
- **优先级**：用户评论指定（「发布到组织 @my-org」/「owner: xxx」）> 仓库配置 > 空=个人
- **API 行为**：
  - POST /api/v1/skills payload 可选 ownerHandle（有值=组织发布，token 须有该组织 publisher 角色）
  - 探测 GET / 下架 DELETE /api/v1/skills/{slug} 带 ?ownerHandle=（组织 skill 必须，否则 404 误判）
  - slug 同一 publisher 内唯一；不支持迁移已有 skill 归属
  - 组织 skill 安装引用：`clawhub install @{owner}/{slug}`
- **影响点**：db_setup 建表 + ALTER 迁移（幂等）；settings.py 保存/读取；event_pipeline 注入 + 评论覆盖；settings.js 输入框

## 5. 对话入口流程（改造后）

```
用户对话「帮我同步 skill 到 ClawHub」
  → process_chat_async（无 scene）→ chat agent
  → chat 技能指引: 同步请求 → 复用 skill 创建的原 Issue（#N）评论「请同步…」
  → webhook → 流水线 → SRE（skill-publish）执行
  → 结果回评 Issue
  → 对话回复「已交办 SRE 处理，结果见 Issue #N」
```

## 6. 不改动（硬约束）

配置页单选模型 / pipeline 路由 / webhook / A2A 机制 / worker 协议 / OBS 分发 / cloud-deploy

## 7. 验证方案

- 单测：seed 补种；_inject_platform_tokens（有/无令牌、sre/非 sre）
- 删除回归：grep 无 skill_sync_scene / sync_service / upsert_bind / skill_sync_bind 残留引用
- 集成：对话同步 → chat 引导 → Issue → SRE skill-publish 执行（ClawHub dry-run 或真实小范围）
- 回归：cloud-deploy 部署任务不受影响
- 前端：配置页出现「技能发布」选项（切换可用）

## 8. 实施顺序

1. 删 scene/sync_service/bind（干净地基）
2. 建 skill-publish 技能 + seed
3. _inject_platform_tokens + user_platform_tokens 扩展
4. chat 技能加指引
5. 重启补种 + 测试 + 验证

## 9. 遗留说明

- docs/obs-multi-skill-redesign.md：OBS 多 skill 改造**暂缓**（skill 单选已确认，OBS 未启用——保留文档待未来需要）
- docs/skill-sync-multiplatform-design.md（v4.7 旧设计）：将被本文档取代（后续清理）
