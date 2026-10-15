-- Issue #343: 一次性强制旧 JWT 失效
--
-- 注意：此 SQL 已集成到 init_database()（db_setup.py），
--       用 meta 表 key='issue343_jwt_invalidated' 标记确保只执行一次。
--       代码部署后 init_database 自动执行，无需人工运行此 SQL。
--       此文件保留作为手动执行备份（如需重置 meta 标记后重跑）。
--
-- 作用：grape_accounts 全表 token_version +1，所有旧 JWT（ver=0）
--       与库中新 ver=1 不匹配 → 401 Session revoked。
-- 效果：所有在线用户被踢下线一次，重新登录后拿新 JWT（sub=新 id），
--       消除旧 JWT sub=旧id 撞上新表有效用户导致的错位写入。
--
-- 安全性：token_version 机制是平台既有语义（改密码/logout 都走这条路），
--         +1 只作废旧 token，不影响数据。
--
-- 回滚：无需回滚。用户重新登录后 token_version 自然匹配。

UPDATE grape_accounts SET token_version = token_version + 1;
