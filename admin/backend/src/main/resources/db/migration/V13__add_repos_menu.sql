-- V13: 新增一级菜单 代码仓管理（Skill 管理之后）
-- 说明: 一级菜单顺序 总览(0) → 运行时管理(1) → Skill 管理(2) → 代码仓管理(3) → AI分析(4) → 系统管理(5)
INSERT INTO admin_permission_menus (code, name, path, parent_id, sort) VALUES ('repos', '代码仓管理', '/repos/manage', 0, 3);
UPDATE admin_permission_menus SET sort = 4 WHERE code = 'ai';
UPDATE admin_permission_menus SET sort = 5 WHERE code = 'sys';
-- 仅 SUPER_ADMIN 默认可见
INSERT INTO admin_role_menu_rel (role_id, menu_id)
SELECT r.id, m.id FROM admin_roles r CROSS JOIN admin_permission_menus m
WHERE r.code = 'SUPER_ADMIN' AND m.code = 'repos';
