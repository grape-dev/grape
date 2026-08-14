-- V2: 菜单改名 设备列表 -> 设备管理（v1 已应用，改名必须走新迁移，不改历史文件）
UPDATE admin_permission_menus SET name = '设备管理' WHERE code = 'runtime.devices';
