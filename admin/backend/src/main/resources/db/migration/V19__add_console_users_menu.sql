-- V19: 系统管理下新增子菜单「控制台用户」（grape 控制台用户管理，即 user_accounts 表）
-- sys 菜单 id=7，现有子菜单 sort: sys.users=1, sys.roles=2 → 新增 console.users sort=3
INSERT INTO admin_permission_menus (code, name, path, parent_id, sort) VALUES
  ('sys.console', '控制台用户', '/sys/console', 7, 3);
