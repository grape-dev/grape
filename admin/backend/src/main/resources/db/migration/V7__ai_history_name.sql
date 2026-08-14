-- V7: 会话名（对齐 VOD 会话列表可重命名）
ALTER TABLE admin_ai_history ADD COLUMN name VARCHAR(100) NULL COMMENT '会话名(默认取首个问题)';
