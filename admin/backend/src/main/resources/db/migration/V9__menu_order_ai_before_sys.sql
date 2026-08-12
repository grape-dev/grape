-- V9: 一级菜单顺序调整为 运行时管理 → AI分析 → 系统管理
-- 说明: V3 曾把 sys 排 2、ai 排 3；现按业务主次改为 ai 排 2、sys 排 3（runtime 保持 1）
UPDATE admin_permission_menus SET sort = 2 WHERE code = 'ai';
UPDATE admin_permission_menus SET sort = 3 WHERE code = 'sys';
