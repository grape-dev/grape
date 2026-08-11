-- V5: AI 会话历史表 + AI 历史菜单（参考 VOD 会话管理，任务后台化配套）
CREATE TABLE IF NOT EXISTS admin_ai_history (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id  VARCHAR(64)  NOT NULL COMMENT 'hermes 会话 id',
    task_id     VARCHAR(64)  NOT NULL COMMENT 'AI 任务 id（幂等去重）',
    query       TEXT         NOT NULL COMMENT '用户问题',
    answer      TEXT         NOT NULL COMMENT 'AI 回答全文',
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    deleted     TINYINT      DEFAULT 0,
    UNIQUE KEY uk_task (task_id),
    KEY idx_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI 助手问答历史';

INSERT INTO admin_permission_menus (code, name, path, parent_id, sort)
SELECT 'ai.history', 'AI 历史', '/ai/history', id, 2 FROM admin_permission_menus WHERE code = 'ai';
