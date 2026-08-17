-- AI 助手会话表（会话 id = session_id（UUID）——绑定 hermes --continue）
CREATE TABLE IF NOT EXISTS admin_ai_chat_session (
    session_id  VARCHAR(64) PRIMARY KEY COMMENT '会话ID(UUID——绑定 hermes --continue)',
    name        VARCHAR(100) NOT NULL DEFAULT '新对话' COMMENT '会话名',
    created_by  VARCHAR(64) NOT NULL COMMENT '创建人(归属校验——登录用户)',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后活动(列表排序)',
    deleted     TINYINT NOT NULL DEFAULT 0 COMMENT '软删(0未删/1已删——统一约定)'
) COMMENT='AI助手会话表';

-- AI 助手消息表（每轮 user + assistant 各一条——task_id 配对——seq 唯一约束兜底）
-- 状态列（send_status/generate_status/error_code/error_msg）随建表一次到位（旧库已存在表时跳过——列已由历史迁移补齐）
CREATE TABLE IF NOT EXISTS admin_ai_chat_message (
    msg_id      VARCHAR(64) PRIMARY KEY COMMENT '消息唯一ID(UUID——幂等)',
    session_id  VARCHAR(64) NOT NULL COMMENT '关联会话ID',
    task_id     VARCHAR(64) NULL COMMENT 'Hermes任务ID(一次提问任务唯一——关联user-assistant——幂等防重)',
    role        VARCHAR(16) NOT NULL COMMENT 'user/assistant(可扩展tool/system)',
    content     TEXT NOT NULL COMMENT 'user:问题文本;assistant:回答原文(含```chart代码块——前端解析渲染)',
    send_status     TINYINT DEFAULT 1 COMMENT '投递状态(user消息专用:0待入库/1成功/2失败——同步写成功恒1——预留扩展)',
    generate_status TINYINT DEFAULT NULL COMMENT 'AI生成状态(assistant专用:0等待/1生成中/2成功/3失败/4中断——完成才写2/3)',
    error_code      VARCHAR(32) NULL COMMENT '失败错误码(超时/限流/模型报错——后台排查)',
    error_msg       VARCHAR(512) NULL COMMENT '失败错误信息(后台排查)',
    seq         BIGINT NOT NULL COMMENT '会话内递增序号(时序——DB MAX(seq)+1——唯一约束兜底)',
    create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted     TINYINT NOT NULL DEFAULT 0 COMMENT '软删(统一约定)',
    INDEX idx_session_seq (session_id, seq),
    INDEX idx_task (task_id),
    UNIQUE KEY uk_session_seq (session_id, seq) COMMENT '会话内 seq 唯一(并发兜底——冲突重试)'
) COMMENT='AI助手消息表';
