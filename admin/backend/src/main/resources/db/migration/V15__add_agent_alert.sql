-- 智能体负载预警值配置表（每个智能体一个阈值百分比，默认 80%）
CREATE TABLE IF NOT EXISTS admin_agent_alert (
    agent_type          VARCHAR(191) NOT NULL PRIMARY KEY COMMENT 'agent_type 外键（对应共享库 agent_type.agent_type）',
    alert_threshold_pct INT          NOT NULL DEFAULT 80 COMMENT '负载率预警阈值百分比 1-100',
    updated_by          VARCHAR(191) NOT NULL DEFAULT '' COMMENT '最后修改人（admin 用户名）',
    updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '智能体负载率预警阈值配置（运营台私有，不落共享库业务）';
