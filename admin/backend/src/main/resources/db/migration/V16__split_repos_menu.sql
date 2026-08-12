-- V16: 代码仓管理改为分组菜单（托管代码仓 + 模板代码仓）
-- repos 一级菜单变为分组（无 path），其下挂两个子菜单
UPDATE admin_permission_menus SET path = '' WHERE code = 'repos';

SET @repos_id = (SELECT id FROM admin_permission_menus WHERE code = 'repos');

INSERT INTO admin_permission_menus (code, name, path, parent_id, sort) VALUES
('managed-repos', '托管代码仓', '/repos/manage', @repos_id, 1),
('template-repos', '模板代码仓', '/templates/manage', @repos_id, 2);

-- SUPER_ADMIN 绑定新子菜单
INSERT INTO admin_role_menu_rel (role_id, menu_id)
SELECT r.id, m.id FROM admin_roles r CROSS JOIN admin_permission_menus m
WHERE r.code = 'SUPER_ADMIN' AND m.code IN ('managed-repos', 'template-repos');
