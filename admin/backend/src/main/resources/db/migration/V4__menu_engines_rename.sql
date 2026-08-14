-- V4: 菜单改名 引擎总览 -> 引擎管理（V1 已应用，改名走新迁移）
UPDATE admin_permission_menus SET name = '引擎管理' WHERE code = 'runtime.engines';
