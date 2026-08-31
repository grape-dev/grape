# Grape 代码托管逻辑与调用链路

## 一、GitCode 登录流程

### 1.1 OAuth 登录链路

```
前端点击"GitCode 登录"
  → GET /api/auth/login/gitcode
  → 读取 meta 表 oauth_gitcode_client_id
  → 生成 state（sha256 随机，防 CSRF）
  → 保存 state 到 DB（save_oauth_state）
  → 返回 GitCode 授权 URL
  → 前端重定向到 https://gitcode.com/oauth/authorize?client_id=xxx&redirect_uri=xxx&state=xxx
```

### 1.2 OAuth 回调处理

```
用户在 GitCode 授权
  → GitCode 回调 GET /api/auth/callback/gitcode?code=xxx&state=xxx

后端处理：
  1. 验证 state（validate_oauth_state，防 CSRF）
  2. POST https://gitcode.com/oauth/token 换 access_token
     - 参数：grant_type=authorization_code, code, client_id, client_secret
  3. GET https://api.gitcode.com/api/v5/user 获取用户信息
     - Header: Authorization: Bearer {access_token}
     - 返回：id, login, nickname
  4. 白名单校验（is_gitcode_user_allowed）
     - 新用户必须在白名单内，否则 403 拒绝
  5. 用户创建/更新
     - 新用户：create_user（随机密码）
     - 老用户：get_user_by_gitcode_id 或 get_user_by_username
  6. link_gitcode_user（存 access_token + refresh_token 到 user_accounts）
  7. 存 unified_user_mapping（GitCode login → user_id 映射，供任务通知解析 assignee）
  8. 签发 JWT
     - access_token（create_access_token）
     - refresh_token（create_refresh_token，HttpOnly cookie）
  9. 重定向到 /?token=xxx&first_login=1（首次登录）或 first_login=0（已有托管仓库）
```

### 1.3 涉及文件

| 文件 | 函数 | 职责 |
|------|------|------|
| src/api/routes/auth.py | gitcode_oauth_login | 生成授权 URL |
| src/api/routes/auth.py | gitcode_oauth_callback | 回调处理 + 用户创建 + JWT 签发 |
| src/user/db.py | save_oauth_state / validate_oauth_state | state 存取 |
| src/user/db.py | link_gitcode_user | 存 GitCode token |
| src/user/db.py | create_user / get_user_by_gitcode_id | 用户创建/查询 |
| src/config.py | is_gitcode_user_allowed | 白名单校验 |
| src/api/jwt_utils.py | create_access_token / create_refresh_token | JWT 签发 |

---

## 二、添加托管机器人流程

### 2.1 飞书机器人注册

```
前端点击"注册飞书机器人"
  → POST /api/feishu-onboard/start
    - body: {bot_type: "chat_bot"/"vod_bot", repo_key}
  → 并发限制：同用户同时只允许一个扫码任务（DB COUNT 检查）
  → 创建 feishu_onboard_task（DB INSERT，status=pending）
  → 启动 _run_registration 异步任务
  → 返回 {task_id, status: "pending"}

前端轮询
  → GET /api/feishu-onboard/status?task_id=xxx
  → 返回 {status, qr_url, expire_in, app_id, error}
```

### 2.2 _run_registration 后台执行

```
1. 飞书 SDK 创建应用
   → lark.aregister_app（飞书 SDK）
   - app_name: "Grape VOD助手"（vod_bot）或 "Grape个人助手"（chat_bot）
   - on_qr_code 回调 → 写入 qr_url → status=waiting_confirm
   - 用户飞书扫码确认
   - SDK 返回 app_id + app_secret + open_id

2. 落库（按 bot_type 区分）
   chat_bot:
     → _apply_chat_credentials(user_id, app_id, app_secret)
     → global_im_bot_config（DB INSERT，is_active=1）
     → chat_agent_session（DB INSERT）
     → 强约束：一个 app_id 全局唯一（查 global_im_bot_config 校验）
     → 换绑清理：旧 app_id 的 bot 被 disable

   vod_bot:
     → _apply_vod_credentials(repo_key, app_id, app_secret)
     → repo_im_group_binding（DB INSERT，关联 repo_key）

3. 绑定飞书身份
   → 用新 bot 通讯录权限读扫码者飞书名
   → update_unified_user_info(username, open_id, feishu_name)
   → GitCode login ↔ 飞书 open_id 映射

4. 连接 bot
   → _connect_bot(bot_type, repo_key, app_id, app_secret, old_app_id)
   → BotManager.add_bot(app_id, app_secret, bot_type)
   → lease 模式：登记到 _eligible_bots（IDLE 状态，不立即连 WS）
   → lease_sweep_loop 扫到新 bot → try_acquire CAS 抢锁
   → 抢到的实例连接 WebSocket → bot 上线
   → 另一个实例 CAS 失败 → 不连
```

### 2.3 涉及文件

| 文件 | 函数 | 职责 |
|------|------|------|
| src/api/routes/feishu_onboard.py | start_onboard | 创建扫码任务 |
| src/api/routes/feishu_onboard.py | _run_registration | 后台注册流程 |
| src/api/routes/feishu_onboard.py | _apply_chat_credentials | chat_bot 落库 |
| src/api/routes/feishu_onboard.py | _apply_vod_credentials | vod_bot 落库 |
| src/api/routes/feishu_onboard.py | _connect_bot | 连接 bot |
| src/im/feishu_async.py | BotManager.add_bot | 登记 bot |
| src/im/feishu_async.py | BotManager.lease_sweep_loop | lease 抢锁 + 连 WS |
| src/im/bot_lease.py | BotLeaseStore.try_acquire | CAS 抢锁 |
| src/session_manager.py | update_unified_user_info | 飞书身份绑定 |

---

## 三、仓库托管逻辑

### 3.1 添加托管仓库

```
用户添加托管仓库
  → 3 步操作：

  1. add_collaborator（GitCode API 邀请 bot 为协作者）
     → PUT https://api.gitcode.com/api/v5/repos/{owner}/{repo}/collaborators/{username}
     → body: {permission: "maintainer"}
     → 幂等：已是成员视为成功

  2. setup_webhook（GitCode API 创建 webhook）
     → find_existing_webhook（检查是否已有同名 webhook）
     → POST https://api.gitcode.com/api/v5/repos/{owner}/{repo}/hooks
     → body: {url: webhook_url, password: webhook_token}
     → webhook_url = http://1.92.115.229/webhook/gitcode
     → 返回 webhook_id

  3. add_managed_repo（DB INSERT）
     → INSERT INTO managed_repos
       (repo_key, repo_owner, repo_name, full_name, webhook_id,
        dev_scene, bot_gitcode_token, gitcode_webhook_token, bot_username)
     → VALUES (...)
```

### 3.2 GitCode 事件处理

```
用户在 GitCode 创建 Issue / Push 代码
  → GitCode 推 webhook
  → POST http://1.92.115.229/webhook/gitcode
  → nginx 80 负载均衡 → A(3001) 或 B(3002)

后端处理：
  1. 解析事件体获取 repo 名
     - payload.project.path_with_namespace
     - 或 payload.repository.full_name

  2. 验证 X-GitCode-Token
     → 查 managed_repos 的 gitcode_webhook_token
     → 未托管仓库拒绝（不在 managed_repos 不接收）

  3. event_pipeline 处理事件
     → issue_opened → VOD Agent 任务
     → issue_updated → Issue Agent 任务
     → issue_comment → 评论同步
     → push → codebase fetch + project-info 刷新
```

### 3.3 飞书消息处理

```
用户在飞书群发消息
  → 飞书通过 WebSocket 推给持有 lease 的实例（A）
  → A 收到消息
  → event_pipeline 处理
  → enqueue_task（打包知识文件 + file_urls）
  → Worker claim
  → Agent 执行

注意：飞书消息不走 webhook，走 WebSocket
  → 只有持有 bot lease 的实例收到消息
  → lease 隔离保证不会两个实例同时处理
```

### 3.4 涉及文件

| 文件 | 函数 | 职责 |
|------|------|------|
| src/user/repo_manager.py | add_collaborator | 邀请 bot 为协作者 |
| src/user/repo_manager.py | setup_webhook | 创建 GitCode webhook |
| src/user/repo_manager.py | find_existing_webhook | 检查已有 webhook |
| src/user/db.py | add_managed_repo | DB 记录托管仓库 |
| src/api/routes/webhook.py | gitcode_webhook | 接收 GitCode webhook |
| src/api/event_pipeline.py | handle_event | 事件分发处理 |

---

## 四、集群部署下的完整链路

### 4.1 请求路由

```
外部请求
  → nginx 80（负载均衡）
  → A(3001) 或 B(3002) 轮询

GitCode webhook
  → nginx 80 → A 或 B 都能处理（DB 共享）

飞书消息
  → WebSocket → 只有持有 lease 的实例（A）收到
  → lease_sweep_loop CAS 抢锁保证只有一个实例连 WS

前端 API
  → nginx 80 → A 或 B 都能处理
```

### 4.2 lease 集群隔离

```
启动时：
  A 和 B 都加载 bot_lease 表 → _eligible_bots（IDLE 状态）

lease_sweep_loop（每 10 秒）：
  扫描 _eligible_bots
  → try_acquire(app_id) CAS 抢锁
  → MySQL: UPDATE bot_lease SET lease_owner=A, lease_expires_at=NOW()+60s
           WHERE app_id=? AND (lease_owner IS NULL OR lease_expires_at < NOW())
  → 抢到 → 连接 WebSocket
  → 没抢到 → 跳过

lease_renew_loop（每 20 秒）：
  续约持有的 lease
  → batch_renew（一条 SQL 批量续约）
  → CAS 失败 → 断开 WebSocket

lease_sweep_loop（每 10 秒）：
  扫描过期 lease → try_acquire 抢占
  → A 挂了 → lease 60 秒过期 → B 抢到 → B 连 WS
```

### 4.3 集群参数

| 参数 | 值 | 说明 |
|------|-----|------|
| TTL | 60s | lease 过期时间 |
| RENEW | 20s | 续约间隔 |
| SWEEP | 10s | 扫描间隔 |
| A 挂了 B 接管 | ≤60s | 最迟 60 秒 B 抢到 lease |

---

## 五、数据存储

### 5.1 MySQL（共享）

| 表 | 用途 |
|----|------|
| user_accounts | 用户账号 + GitCode token |
| managed_repos | 托管仓库列表 |
| global_im_bot_config | 飞书 bot 配置 |
| repo_im_group_binding | VOD bot ↔ repo 绑定 |
| bot_lease | bot lease 分布式锁 |
| feishu_onboard_task | 飞书扫码任务 |
| agent_shared_knowledge | 知识库（含 obs_key） |
| agent_tasks | Agent 任务队列 |
| agent_runtimes | Worker 注册 |
| unified_user_mapping | GitCode ↔ 飞书用户映射 |
| meta | 全局配置（OAuth client_id 等） |

### 5.2 OBS（共享）

| 对象 | 用途 |
|------|------|
| knowledge/{owner}/{repo}/... | 知识文件 |
| project-info/{repo_key}/... | 基线文件 |
| test/... | 临时测试对象 |

### 5.3 本地磁盘（不共享，但同机集群共享）

| 路径 | 用途 | 跨机影响 |
|------|------|---------|
| /home/yangaiwu/grape/ | 代码 + skills + configs | 不影响（同份代码） |
| /home/yangaiwu/grape-workspaces/ | git 仓库 + project-info | lease 隔离不走 B |
| /tmp/ | nginx temp + 临时文件 | 不影响 |
