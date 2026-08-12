-- 对齐 VOD：AI 菜单收敛为单入口（AI分析 → /ai/assistant），移除独立「AI 历史」子菜单
-- 历史功能内嵌在 AI 助手页会话列表（分析报表区），无需独立菜单入口
DELETE FROM admin_permission_menus WHERE id = 10 AND path = '/ai/history';
-- AI分析 顶级菜单直接指向 AI 助手页（对齐 VOD ai-query 顶级直达）
UPDATE admin_permission_menus SET path = '/ai/assistant' WHERE id = 5 AND code = 'ai';
