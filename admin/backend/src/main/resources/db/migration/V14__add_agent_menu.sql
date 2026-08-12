-- V14: 新增一级菜单 智能体管理（紧挨总览之后、运行时管理之前）
-- 说明: 一级菜单顺序 总览(0) → 智能体管理(1) → 运行时管理(2) → Skill 管理(3) → 代码仓管理(4) → AI分析(5) → 系统管理(6)
INSERT INTO admin_permission_menus (code, name, path, parent_id, sort) VALUES ('agent', '智能体管理', '/agent/manage', 0, 1);
UPDATE admin_permission_menus SET sort = 2 WHERE code = 'runtime';
UPDATE admin_permission_menus SET sort = 3 WHERE code = 'skill';
UPDATE admin_permission_menus SET sort = 4 WHERE code = 'repos';
UPDATE admin_permission_menus SET sort = 5 WHERE code = 'ai';
UPDATE admin_permission_menus SET sort = 6 WHERE code = 'sys';
-- 仅 SUPER_ADMIN 默认可见（其他角色需在角色权限配置中自行绑定）
INSERT INTO admin_role_menu_rel (role_id, menu_id)
SELECT r.id, m.id FROM admin_roles r CROSS JOIN admin_permission_menus m
WHERE r.code = 'SUPER_ADMIN' AND m.code = 'agent';
