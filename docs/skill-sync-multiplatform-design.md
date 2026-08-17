# Skill 多平台同步 — 设计 v4.0（定稿讨论版）

状态: 交互设计已确认，待实现
特性分支: feat/skill-sync-multiplatform
前置调研: docs/clawhub-publish-research.md

## 1. 需求

开发者在 grape 平台完成 skill 开发后，通过**聊天**指示系统同步到外部平台：

```
① 开发者建 Issue（如「创建查询华为云VPC列表的skill」）
② grape 全流程自动完成 skill 创建
③ 开发者聊天:「把这个 skill 同步到 ClawHub」
④ 系统确认 → 自动发布到目标平台
⑤ 回复开发者: 成功 + 链接 / 失败 + 原因
```

## 2. 核心决策（用户已确认）

| 决策点 | 结论 |
|---|---|
| 触发方式 | 聊天指令（三个入口） |
| 入口 | ① Issue 评论 ② 飞书/IM ③ 管理台网页对话（全部现有链路） |
| 同步对象 | 创建 skill 的 **Issue 关联的 skill**（不指名，按对话上下文定位） |
| 发布账号 | **开发者自己的平台账号**（每用户独立令牌） |
| 平台选择 | 一次支持说多个；只返回当前支持（有插件）的平台 |
| 未配置令牌 | 返回「未配置 + 如何配置」指引 |
| 发布前确认 | ✅ 需要二次确认 |
| 重复同步 | 提示「已存在」→ 询问是否覆盖更新 |

## 3. 交互流程

### 3.1 入口 → 意图识别

| 入口 | 用户说法 | 处理技能 |
|---|---|---|
| Issue 评论 | 「请同步到 ClawHub」 | issue 技能（新场景） |
| 飞书/IM | 「把这个 skill 同步到 ClawHub」 | chat 技能（新场景 skill-sync） |
| 管理台网页 | 「同步 skill 到 ClawHub」 | chat 技能（同飞书） |

### 3.2 发布确认（二次确认）

```
用户: 「同步到 ClawHub」
系统: 「确认将 huaweicloud-vpc-list 同步到 ClawHub？回复『确认』执行」
用户: 「确认」
系统: 执行发布 → 回复结果
```

### 3.3 平台处理

| 情况 | 响应 |
|---|---|
| 平台有插件 + 已配令牌 | 正常发布 |
| 平台有插件 + 未配令牌 | 「ClawHub 未配置：请在设置页填写令牌（获取方式: 访问 clawhub.ai/cli/auth → GitHub 登录 → 复制 clh_ 令牌）」 |
| 平台无插件（不支持） | 「暂不支持 xxx 平台」 |

### 3.4 重复同步

```
系统: 「该 skill 已在 ClawHub 存在（v1.0.0），是否覆盖更新？回复『覆盖』」
用户: 「覆盖」
系统: 发布新版本 → 回复新链接
```

## 4. 令牌模型（多用户隔离）

- user_accounts 表新增 `clawhub_token`（及未来 github/gitee token）列
- 开发者自行获取: 浏览器访问 https://clawhub.ai/cli/auth → GitHub 登录 → clh_ 令牌
- 配置入口: 管理台 Settings → Credentials → ClawHub 区块
- **发布用谁的令牌 = 发布到谁名下**
- 安全: 加密存储，不进 git/日志/评论

## 5. 插件架构

```
src/sync/                      # 主框架: 接口 + 加载器
├── __init__.py                # SkillPublisher 抽象接口
├── loader.py                  # 扫描 plugins/ 加载 enabled 插件
└── events.py                  # 结果写事件表

plugins/sync-clawhub/          # 平台插件（各一目录）
plugins/sync-github/
plugins/sync-gitee/
```

- manifest.json: name/version/enabled/required_env
- 令牌注入: 框架从开发者 user_accounts 读 → req.env（不落盘）
- 加新平台 = 加一个插件目录

## 6. 待实现清单

- [ ] chat 技能新增 skill-sync 场景（识别 + 确认 + 结果回复）
- [ ] issue 技能支持同步指令（Issue 评论入口）
- [ ] src/sync/ 主框架（接口 + 加载器 + 事件打标）
- [ ] plugins/sync-clawhub/（第一个插件，POST /api/v1/skills）
- [ ] user_accounts 加 clawhub_token 列 + 设置页配置区块
- [ ] 定位「Issue 关联的 skill」（最近完成事件 → 仓库 + skill 目录）
- [ ] 重复检测（slug 已存在 → 覆盖更新流程）
- [ ] GitHub/Gitee 插件（后续）
