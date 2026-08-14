-- ============================================================
-- Grape 运营管理台 admin 自建表（与 grape 共享同一 MySQL，账号体系完全隔离）
-- 约定：admin_* 表统一五件套 created_at/created_by/updated_at/updated_by/deleted（软删除）
-- created_by/updated_by = 操作者 admin 用户名；updated_at 由 MySQL ON UPDATE 自动维护
-- audit_log 保持 append-only（仅 created_at）
-- ============================================================

CREATE TABLE admin_users (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  username VARCHAR(64) NOT NULL,
  password_hash VARCHAR(100) NOT NULL,
  nickname VARCHAR(64) DEFAULT '',
  status TINYINT DEFAULT 1,              -- 1 启用 0 禁用
  deleted TINYINT DEFAULT 0,             -- 软删除：0 未删除 1 已删除
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',     -- 创建人（admin 用户名）
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',     -- 最后更新人（admin 用户名）
  UNIQUE KEY uk_users_username (username, deleted)   -- 软删除下用户名可复用
);

CREATE TABLE admin_roles (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  code VARCHAR(64) NOT NULL,             -- SUPER_ADMIN / VIEWER / OPS / 自定义
  name VARCHAR(64) NOT NULL,
  is_protected TINYINT DEFAULT 0,
  deleted TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',
  UNIQUE KEY uk_roles_code (code, deleted)
);

CREATE TABLE admin_permission_menus (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  code VARCHAR(64) NOT NULL,             -- runtime.devices / runtime.engines / runtime.credentials / ai.assistant / sys.users / sys.roles
  name VARCHAR(64) NOT NULL,
  path VARCHAR(128) NOT NULL,            -- /runtime/devices 等前端路由
  parent_id BIGINT DEFAULT 0,
  sort INT DEFAULT 0,
  deleted TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',
  UNIQUE KEY uk_menus_code (code, deleted)
);

CREATE TABLE admin_role_menu_rel (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,  -- 软删除下用自增主键
  role_id BIGINT NOT NULL,
  menu_id BIGINT NOT NULL,
  deleted TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',
  UNIQUE KEY uk_rm (role_id, menu_id, deleted)
);

CREATE TABLE admin_user_role_rel (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT NOT NULL,
  role_id BIGINT NOT NULL,
  deleted TINYINT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  created_by VARCHAR(64) DEFAULT '',
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  updated_by VARCHAR(64) DEFAULT '',
  UNIQUE KEY uk_ur (user_id, role_id, deleted)
);

CREATE TABLE admin_audit_log (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id BIGINT,
  username VARCHAR(64),
  action VARCHAR(64) NOT NULL,           -- credential.issue / credential.revoke / device.cleanup / engine.delete / task.cleanup / user.* / role.*
  target VARCHAR(255),
  detail VARCHAR(1024),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ============ 种子数据 ============

-- 角色（保护角色不可改删）
INSERT INTO admin_roles (code, name, is_protected) VALUES
 ('SUPER_ADMIN', '超级管理员', 1),
 ('VIEWER', '只读', 1),
 ('OPS', '运营', 0);

-- 菜单（结构与设计文档 §5 一致：运行时管理 / AI / 系统管理）
INSERT INTO admin_permission_menus (code, name, path, parent_id, sort) VALUES
 ('runtime', '运行时管理', '', 0, 1),
 ('runtime.devices', '设备列表', '/runtime/devices', 1, 1),
 ('runtime.engines', '引擎总览', '/runtime/engines', 1, 2),
 ('runtime.credentials', '凭据管理', '/runtime/credentials', 1, 3),
 ('ai', 'AI', '', 0, 2),
 ('ai.assistant', 'AI 助手', '/ai/assistant', 5, 1),
 ('sys', '系统管理', '', 0, 3),
 ('sys.users', '用户管理', '/sys/users', 7, 1),
 ('sys.roles', '角色权限配置', '/sys/roles', 7, 2);

-- SUPER_ADMIN 挂全部菜单
INSERT INTO admin_role_menu_rel (role_id, menu_id)
SELECT r.id, m.id FROM admin_roles r CROSS JOIN admin_permission_menus m
WHERE r.code = 'SUPER_ADMIN';

-- VIEWER 只读菜单（设备列表/引擎总览/AI 助手）
INSERT INTO admin_role_menu_rel (role_id, menu_id)
SELECT r.id, m.id FROM admin_roles r JOIN admin_permission_menus m
  ON m.code IN ('runtime.devices', 'runtime.engines', 'ai.assistant')
WHERE r.code = 'VIEWER';
