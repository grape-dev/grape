-- V11: 新增一级菜单 Skill 管理（紧挨运行时管理之后）
-- 说明: 一级菜单顺序 总览(0) → 运行时管理(1) → Skill 管理(2) → AI分析(3) → 系统管理(4)
INSERT INTO admin_permission_menus (code, name, path, parent_id, sort) VALUES ('skill', 'Skill 管理', '/skill/manage', 0, 2);
UPDATE admin_permission_menus SET sort = 3 WHERE code = 'ai';
UPDATE admin_permission_menus SET sort = 4 WHERE code = 'sys';
-- 仅 SUPER_ADMIN 默认可见（其他角色需在角色权限配置中自行绑定）
INSERT INTO admin_role_menu_rel (role_id, menu_id)
SELECT r.id, m.id FROM admin_roles r CROSS JOIN admin_permission_menus m
WHERE r.code = 'SUPER_ADMIN' AND m.code = 'skill';
