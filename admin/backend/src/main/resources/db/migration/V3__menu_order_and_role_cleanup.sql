-- V3: 菜单排序调整（AI 移到系统管理后面）+ 移除 OPS 运营角色
-- 说明: V1 已应用，历史迁移文件不可改，本迁移保证新装环境与现网行为一致
UPDATE admin_permission_menus SET sort = 2 WHERE code = 'sys';
UPDATE admin_permission_menus SET sort = 3 WHERE code = 'ai';
UPDATE admin_roles SET deleted = 1 WHERE code = 'OPS';
