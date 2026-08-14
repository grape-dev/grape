-- V6: AI 自定义模板表（参考 VOD t_ai_query_template）
CREATE TABLE IF NOT EXISTS admin_ai_template (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    template_name    VARCHAR(50)  NOT NULL COMMENT '模板名称',
    template_content VARCHAR(600) NOT NULL COMMENT '模板内容(提示词)',
    created_by       VARCHAR(64)  NOT NULL COMMENT '创建人用户名',
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted          TINYINT      NOT NULL DEFAULT 0,
    UNIQUE KEY uk_creator_name (created_by, template_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI自定义模板表';
