# 去除设置页平台 Token 配置——统一走知识库 JSON 设计

## 一、背景

当前平台发布凭据（ClawHub/GitHub/GitCode Token + 发布仓库 + OwnerHandle）有两条传递路径：
1. 设置页 → repo_platform_tokens 表 → _inject_platform_tokens 注入环境变量 → SRE 读环境变量
2. 知识库 repo-credentials.json → .project-info/ 物化 → SRE 读 JSON 回退

路径 1 是 v4.2 引入的旧机制，路径 2 是这次新增的。两条路径并存导致用户要在两个地方配凭据，且 _inject_platform_tokens 注入空值时 SRE 要判断"空值=缺失"不够明确。

统一去掉路径 1，只保留路径 2（知识库 JSON）。

## 二、改动文件清单

| 文件 | 改动 |
|------|------|
| src/api/routes/settings.py | 删除平台 token 相关的 API 读写逻辑和 7 个函数 |
| src/api/event_pipeline.py | 删除 _inject_platform_tokens 函数和 2 个调用点 |
| src/api/static/js/pages/settings.js | 删除 ClawHub/GitHub/GitCode 三个 UI 区块和 save 逻辑 |
| src/api/db_setup.py | 删除 repo_platform_tokens 建表语句和索引 |

## 三、具体改动

### 3.1 settings.py

删除函数：
- _get_platform_token
- _get_repo_platform_token
- _get_repo_owner_handle
- _get_repo_publish_repo
- _save_repo_platform_token
- _save_repo_publish_repo
- _save_repo_owner_handle

get_credentials 返回值去掉：
- clawhub 区块（token_exists / owner_handle）
- github 区块（token_exists / publish_repo）
- gitcode 区块（publish_token_exists / publish_repo）

UpdateCredentials 模型去掉字段：
- clawhub_token
- clawhub_owner_handle
- github_token
- gitcode_token
- github_repo
- gitcode_repo

update_credentials 去掉：
- 写 repo_platform_tokens 的逻辑（clawhub_token / github_token / gitcode_token）
- 写 publish_repo 的逻辑（github_repo / gitcode_repo）
- 写 owner_handle 的逻辑（clawhub_owner_handle）

保留：
- huawei_cloud_ak / huawei_cloud_sk 读写
- codearts_ak / codearts_sk 读写
- gitcode API token（OAuth）只读回显
- repo_key 字段（AK/SK 仍按仓库维度）

### 3.2 event_pipeline.py

删除：
- _inject_platform_tokens 函数（第 894-938 行）
- 第 802 行调用点（sre 任务注入）
- 第 1080 行调用点（sre direct 模式注入）

保留：
- _inject_cloud_credentials（HUAWEICLOUD_SDK_AK/SK）— 读 user_accounts 表
- BOT_GITCODE_TOKEN — 读 managed_repos 表

### 3.3 settings.js

删除 UI 区块：
- ClawHub 区块（Bearer Token + Owner Handle 输入框）
- GitHub 区块（Token + 发布目标仓库输入框）
- GitCode 区块（Token + 发布目标仓库输入框）

删除 save 逻辑：
- clawhubToken / clawhubOwnerHandle 收集
- githubToken / githubRepo 收集
- gitcodeToken / gitcodeRepo 收集

保留：
- GitCode API（OAuth）只读回显
- Huawei Cloud AK/SK 输入框和 save

### 3.4 db_setup.py

删除：
- repo_platform_tokens 建表语句
- idx_repo_platform 索引
- owner_handle 列迁移语句

## 四、不受影响

- _inject_cloud_credentials（HUAWEICLOUD_SDK_AK/SK）
- BOT_GITCODE_TOKEN（managed_repos.bot_gitcode_token）
- skill-publish/SKILL.md（已是"优先环境变量，缺失时回退 JSON"）
- AK/SK 设置页配置
- GitCode API Token（OAuth）设置页
- repo_platform_tokens 表数据（不主动删数据，只是不再建表和读写）

## 五、功能影响

- SRE 不再从环境变量收到 CLAWHUB_TOKEN/GITHUB_TOKEN/GITCODE_TOKEN/publish_repo/owner_handle
- SRE 从 .project-info/repo-credentials.json 读取这些值
- 如果 JSON 缺失，SRE 报"未配置"跳过该平台

## 六、验证方式

1. 删除后触发一次 SRE 发布任务
2. 确认 SRE 的 env_extra 里不含 CLAWHUB_TOKEN/GITHUB_TOKEN/GITCODE_TOKEN
3. 确认 SRE 从 .project-info/repo-credentials.json 读到凭据
4. 确认发布成功
