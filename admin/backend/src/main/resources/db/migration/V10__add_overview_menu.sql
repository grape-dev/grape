-- 运营总览：新增一级菜单「总览」，排最前（sort=0）
INSERT INTO admin_permission_menus (code, name, path, parent_id, sort)
VALUES ('overview', '总览', '/overview', 0, 0);

-- SUPER_ADMIN / VIEWER 挂总览菜单
INSERT INTO admin_role_menu_rel (role_id, menu_id)
SELECT r.id, m.id FROM admin_roles r CROSS JOIN admin_permission_menus m
WHERE r.code IN ('SUPER_ADMIN', 'VIEWER') AND m.code = 'overview';
