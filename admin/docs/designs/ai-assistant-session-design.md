# AI 助手会话存储重构设计（GRP-DESIGN v4）

> 文档编号：GRP-DESIGN-AI-ASSISTANT-001
> 状态：评审中（未实施）
> 分支：feat/ai-assistant-optimize（基于 dev c229d0a）
> 背景：AI 助手会话存储重构——对齐 VOD 会话模式（前端不感知 hermes sessionId——会话 id 即绑定名）——消息分条 + 游标分页 + task_id 幂等配对

## 一、核心设计

1. 会话 id = 会话表 session_id（UUID——绑定 hermes --continue——前端不感知 hermes 内部 sessionId）
2. hermes 绑定：--continue <会话 UUID>（同名创建/续接——内部记住上下文——多轮连续）
3. 前端只传会话 id（不传 hermes sessionId）——只管历史记录
4. 归属校验：会话 created_by = 当前登录用户（JWT）——403 拒绝
5. 存储：admin_ai_chat_session + admin_ai_chat_message（分条——每轮 user + assistant 各一条——task_id 配对）
6. 消息分页：游标分页（首次 40——滚动 20——seq 游标——不用 offset）
7. beta 截断：老表 admin_ai_history 不再读写（数据丢弃——干净切换）

## 二、表结构

```sql
-- 会话表（admin 运营管理台）
CREATE TABLE admin_ai_chat_session (
    session_id  VARCHAR(64) PRIMARY KEY COMMENT '会话ID(UUID——绑定 hermes --continue)',
    name        VARCHAR(100) NOT NULL DEFAULT '新对话' COMMENT '会话名',
    created_by  VARCHAR(64) NOT NULL COMMENT '创建人(归属校验——登录用户)',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后活动(列表排序)',
    deleted     TINYINT NOT NULL DEFAULT 0 COMMENT '软删(0未删/1已删——统一约定)'
) COMMENT='AI助手会话表';

-- 消息表（每轮 user + assistant 各一条——task_id 配对）
CREATE TABLE admin_ai_chat_message (
    msg_id      VARCHAR(64) PRIMARY KEY COMMENT '消息唯一ID(UUID——幂等)',
    session_id  VARCHAR(64) NOT NULL COMMENT '关联会话ID',
    task_id     VARCHAR(64) NULL COMMENT 'Hermes任务ID(一次提问任务唯一——关联user-assistant——幂等防重)',
    role        VARCHAR(16) NOT NULL COMMENT 'user/assistant(可扩展tool/system)',
    content     TEXT NOT NULL COMMENT 'user:问题文本;assistant:回答原文(含```chart代码块——前端解析渲染)',
    send_status     TINYINT DEFAULT 1 COMMENT '投递状态(user消息专用:0待入库/1成功/2失败——同步写成功恒1——预留扩展)',
    generate_status TINYINT DEFAULT NULL COMMENT 'AI生成状态(assistant专用:0等待/1生成中/2成功/3失败/4中断——完成才写2/3)',
    error_code      VARCHAR(32) NULL COMMENT '失败错误码(超时/限流/模型报错——后台排查)',
    error_msg       VARCHAR(512) NULL COMMENT '失败错误信息(后台排查——assistant失败时记录)',
    seq         BIGINT NOT NULL COMMENT '会话内递增序号(时序——user/assistant都递增——assistant seq>对应user seq)',
    create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted     TINYINT NOT NULL DEFAULT 0 COMMENT '软删(统一约定)',
    INDEX idx_session_seq (session_id, seq),
    INDEX idx_task (task_id),
    UNIQUE KEY uk_session_seq (session_id, seq) COMMENT '会话内 seq 唯一(并发兜底——冲突重试)'
) COMMENT='AI助手消息表';
```

## 三、消息配对与写入机制（task_id 幂等——核心）

### 配对规则

- 一轮对话任务：**1 条 user 消息 + 最多 1 条 assistant 消息共用同一 task_id**
- 时序：seq 严格递增——assistant 的 seq 一定 > 对应 user 的 seq
- **禁止**通过「取最后一条 user 消息」配对 assistant（异步乱序/多端并发会导致归属错误——必须 task_id 显式绑定）

### 时序形态

```
seq=1 user       taskId=T001
seq=2 assistant  taskId=T001
seq=3 user       taskId=T002
seq=4 assistant  taskId=T002
```

### 写入时机

1. user 消息：提交时【同步入库】（task_id 由服务端生成——AI 接口层统一生成 task_id=UUID）
2. assistant 消息：Hermes 任务【最终闭环时】（成功/超时/中断/报错）统一入库——SSE 流式期间【不提前插占位】——流关闭后组装完整内容入库

### 幂等保障（防重复写入 assistant）

```
入库 assistant 前校验：
  SELECT 1 FROM admin_ai_chat_message
  WHERE session_id=? AND task_id=? AND role='assistant' AND deleted=0;
- 已有记录：UPDATE 覆盖（重试成功场景）
- 无记录：INSERT 新增
```

### 失败/重试规则

- 失败：插入失败提示消息（assistant——content="（查询失败，可重试）"——task_id=T001）
- 重试：**复用同一 taskId**——不新增 user 消息——Hermes --continue 重新执行
- 重试成功：按 session_id+task_id+role=assistant 查到失败消息 → UPDATE 覆盖 content
- 约束：只允许对【失败标记】的 assistant 消息 UPDATE——正常消息禁止覆盖
- 保证：一轮任务永远只有一条 assistant（不会重试多行）

### seq 生成（无 Redis——方案 A：DB MAX(seq)+1 + 唯一约束）

```
取 seq：
  SELECT COALESCE(MAX(seq), 0) + 1 FROM admin_ai_chat_message
  WHERE session_id = ? AND deleted = 0;
→ 插入（seq = MAX+1）

并发兜底（唯一约束）：
  UNIQUE KEY uk_session_seq (session_id, seq)
→ 极端并发（同 seq 冲突）——插入失败——重试（重取 MAX+1）

安全性（当前场景）：
  · 前端 isQuerying 串行（同一会话不同时提问）
  · 后端 slots=3（限流）
  · 同会话并发极低——MAX+1 串行下必然正确
  · 唯一约束兜底（万一并发——冲突重试——不产生重复 seq）

不使用 Redis（当前环境无 Redis）——方案 A 等价可靠
（正常路径一致——唯一约束保证不重复）
```

## 三.5 任务状态持久化（status 查库兜底——解决 404）

【红线】AI 响应失败 ≠ 用户消息失败——user 不标失败（业务语义不混淆）

【状态字段（按 role 分用）】
```
· role=user：只 send_status=1（同步写入成功——不标失败）
· role=assistant：只 generate_status=2（成功）/3（失败）
  + error_code/error_msg（失败原因——超时/限流/模型报错——
  后台排查）
```

【写入时机】
```
· submit：写 user 消息 send_status=1
· saveHistory（任务完成）：写 assistant 消息
   generate_status=2（成功——content=回答）/
   3（失败——content="（查询失败，可重试）" + error_code/error_msg）
```

【status 接口（判断顺序：内存 → 库）】
```
· 内存 tasks Map 有 → 实时状态（running/done/error）
· 内存无（清理/重启）→ 查库（该 task_id 的消息）：
   有 assistant（generate_status=2/3）→ done/error【不 404】
   有 user 无 assistant（任务中断——assistant 未写）→
     error"任务中断，请重新提问"
   无任何记录（假 id）→ 200 {status:"not_found"}
→ 任务状态持久化（清理/重启后仍可查——不再 HTTP 404 噪音）
```

## 四、消息分页（游标分页——40/20）

### 场景 A：打开会话首次加载（最新 40 条）

```sql
SELECT msg_id, role, content, seq, create_time FROM admin_ai_chat_message
WHERE session_id=? AND deleted=0
ORDER BY seq DESC LIMIT 40;
```
→ 后端内存反转（seq 升序）返回（前端时序正常）→ 前端记录 minSeq

### 场景 B：滚动加载更早历史（每次 20 条）

```sql
SELECT ... WHERE session_id=? AND deleted=0 AND seq < #{minSeq}
ORDER BY seq DESC LIMIT 20;
```
→ 反转 → 拼到列表头部 → 更新 minSeq

### 边界

- 返回行数 < limit → hasMore=false（无更早——关闭加载）
- (session_id, seq) 联合索引命中——深度分页稳定（不用 offset）

## 五、接口规范

```
POST /api/admin/ai/chat/submit
  新会话：{query} → 建会话（UUID）→ 生成 task_id → 写 user 消息
          （seq=1）→ 脚本 --continue <session_id> → 响应
          {taskId, sessionId}
  继续：{query, sessionId} → 校验（存在+归属 403）→ 生成 task_id →
        写 user 消息（seq 递增）→ --continue 同 id
  完成：写 assistant 消息（成功/失败——task_id 幂等）

GET /api/admin/ai/chat/sessions?limit=&offset=
  → 会话列表（created_by=当前用户——updated_at 降序——
    轻量——分页 limit/offset——初期会话少够用）

GET /api/admin/ai/chat/session/{id}/messages?cursorSeq=&limit=
  → 校验归属（403）→ 游标分页
  → 响应：
  {
    "code": 0,
    "data": {
      "sessionId": "sess-xxx",
      "messages": [{msgId, role, content, taskId, seq, createTime}...],
      "hasMore": true,
      "minSeq": 120
    }
  }

PUT /api/admin/ai/chat/session/{id}/name（新增——重命名）
  → 校验归属（403）→ 更新 name

DELETE /api/admin/ai/chat/session/{id}（新增——删除）
  → 校验归属（403）→ 软删会话 deleted=1 + 消息 deleted=1
```

## 六、会话流程（多轮——hermes 绑定）

```
【新会话】提交 {query} → 建会话 → task_id → user 消息 →
  --continue <session_id> → 完成写 assistant（成功/失败）

【继续对话】提交 {query, sessionId} → 校验（存在+归属 403）→
  task_id → user 消息 → --continue 同 id（内部记住——多轮连续）
  → 完成写 assistant

【前端不感知 hermes sessionId】
  会话 id = 会话表 UUID（前端只管这个——提交/历史/归属）
```

## 七、流式实时（不走分页）

- 提问/AI 回复——SSE 推送（现链路 submit→streamTask——token 流累积）——完成写 assistant 消息（不提前插占位）
- 前端流式 append——分页只加载存量历史（打开/滚动）

## 八、边界与独立逻辑

- 前端分页 = 界面历史展示（40/20）；Hermes 上下文 = 服务端 --continue 内部管理（LLM 消息子集）——两套独立
- 不做无限下拉（返回 < size → 关闭加载更多）
- 消息量极大时（可选预案）：轻量列表接口（只 msgId/seq/role/createTime）——现阶段 40/20 够用
- 并发保障：前端 isQuerying 串行（第一层）+ 后端 slots=3 限流（第二层）——seq 用 DB MAX(seq)+1 + 唯一约束（无 Redis——方案 A）
- 未来多用户协同/多终端对话：需分布式锁锁会话任务入口——当前场景不需要

## 九、前端分页交互

- 初始化：拉最新 40 条 → 自动滚动到底部（聚焦最新）
- 主触发：向上滚动触达容器顶部阈值 → 自动加载更早 20 条
- 兜底：顶部常驻【加载更多历史】按钮（滚动监听失效/移动端兼容/消息少无法滚动时）
- hasMore=false → 关闭加载

## 十、脚本改动详情（ai_hermes_query.py）

| 项 | 原实现 | 新实现 | 原因 |
|---|---|---|---|
| 会话绑定 | --resume <hermes 内部 sessionId> | --continue <会话 UUID>（前端会话 id 即绑定名——hermes 同名创建/续接） | 前端不感知 hermes 内部 ID |
| 会话 id 获取兜底 | sessions list --source cli --limit 1（并发取错——多轮断裂根因） | 【删除】——--continue 绑定后不需要 | 兜底不可靠——按名绑定 |
| session 事件（emit） | 流内 emit session 事件 | 【删除】——前端从响应/历史拿会话 id | 前端不需要流内事件 |
| 工作区 cwd | 继承主服务目录 | /tmp/ai-hermes-ws（专属目录——隔离） | 会话 workspace 隔离 |
| 兼容过渡 | - | 切换前保留 session 事件——切换后删 | 发布无感——beta 截断后清理 |

## 十一、改动点

| 层 | 改动 |
|---|---|
| 数据库 | 建表 ×2（V17 迁移——admin 重启 Flyway 自动执行） |
| 后端 | submit（建会话/task_id/写消息/校验归属/响应 sessionId）；新接口 sessions（分页）/messages（游标分页）/rename/delete；saveHistory → 写消息（task_id 幂等——成功/失败）；JWT → created_by；task_id 服务端生成 |
| 脚本 | ai_hermes_query.py --continue <会话 id>（详情见上） |
| 前端 | 新会话（无 id 提交→响应拿 sessionId）；提交 sessionId: session.id；历史列表新接口；消息游标分页（40/20——minSeq/hasMore——滚动+按钮）；流式 SSE append；重试（复用 taskId——UPDATE 覆盖失败消息）；删 session 事件处理；loadSessions 合并简化；图表 renderParts 解析（segFor——已有） |

## 十二、影响

- 前端彻底不感知 hermes sessionId（--continue 绑定）
- 分页稳定（游标——深会话不慢）
- 归属可控（403——跨用户拒绝）
- 消息成对（task_id 幂等——一轮一条 assistant——重试不重复）
- beta 截断（干净——一套逻辑）
- 流式实时（SSE append）
- 现有服务/其他 admin 功能不受影响（AI 助手独立）

## 十三、验证方案

1. 脚本：--continue 同名两轮上下文连续（已实测 ✓）
2. 接口：submit 无 id → 返回 id → 再带 id → 续接（DB 同 session 多 task_id 成对——seq 递增）→ 分页（40/20——hasMore/minSeq）
3. 幂等：同 task_id 重复闭环 → 不重复插 assistant（UPDATE 覆盖）
4. 权限：他人 sessionId → 403
5. 前端：新会话 → 多轮 → 刷新 → 历史恢复 → 续接 → 分页滚动 → 图表渲染 → 重试（覆盖失败消息）
6. 回归：hermes verify 基线（blocker 已知）

## 十四、实施顺序

1. 建表（V17——admin 重启 Flyway 自动执行）
2. 后端（submit/sessions/messages/rename/delete/归属/task_id）
3. 脚本（--continue——切换时改）
4. 前端（会话 id/历史/分页/流式/重试/删 session 事件）
5. 部署验证（前端构建 + 后端重启 + 实测）

## 十五、待确认

1. 整体方案确认？
2. task_id 字段 + seq 生成（DB MAX(seq)+1 + 唯一约束——无 Redis）——确认？
3. 重试交互（复用 taskId——UPDATE 覆盖失败消息）——确认？
4. 列表分页（limit/offset）——确认？
5. 实施顺序确认？
