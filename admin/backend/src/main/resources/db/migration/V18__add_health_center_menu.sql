-- V18: 新增一级菜单「健康中心」，位于代码仓管理(repos sort=4)和AI分析(ai sort=5)之间
-- 一级菜单顺序: 总览(0) → 智能体管理(1) → 运行时管理(2) → Skill管理(3) → 代码仓管理(4) → 健康中心(5) → AI分析(6) → 系统管理(7)
INSERT INTO admin_permission_menus (code, name, path, parent_id, sort) VALUES ('health', '健康中心', '/health/center', 0, 5);
UPDATE admin_permission_menus SET sort = 6 WHERE code = 'ai';
UPDATE admin_permission_menus SET sort = 7 WHERE code = 'sys';

-- SUPER_ADMIN 默认可见
INSERT INTO admin_role_menu_rel (role_id, menu_id)
SELECT r.id, m.id FROM admin_roles r CROSS JOIN admin_permission_menus m
WHERE r.code = 'SUPER_ADMIN' AND m.code = 'health';
