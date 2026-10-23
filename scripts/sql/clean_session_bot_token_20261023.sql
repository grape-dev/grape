-- R25.9 回声治理：清理 devops_agent_session 中被低级别 token 污染的行
-- 背景：绑定仓库/保存 Agent 配置/应用场景三条路径曾把 bot 级 token 回填进
-- repo_gitcode_token，导致 token 解析第 1 级(session)命中 bot token，
-- 自循环检测放行 bot 评论 → bot 触发流水线造成回声。
-- 写入端已在代码层修复（upsert 传 None=保留），本 SQL 清存量污染。
--
-- 判定标准：值等于全局 bot token 或任一仓库 bot token → 污染，清空。
-- 不做"全清"，防止误伤确实配了专属 token 的仓库（如个人 token 配的 session 级）。
--
-- 执行前先备份。在部署环境的 MySQL 实例上执行（连接信息见部署配置，不入库）。

-- 1. 备份
CREATE TABLE backup_das_token_20261023 AS
  SELECT * FROM devops_agent_session WHERE repo_gitcode_token != '' OR repo_github_token != '';

-- 2. 清 repo_gitcode_token（值等于 bot token 的污染行）
UPDATE devops_agent_session d
SET d.repo_gitcode_token = ''
WHERE d.repo_gitcode_token != ''
  AND (
    d.repo_gitcode_token = (SELECT value FROM meta WHERE `key`='global_bot_gitcode_token')
    OR d.repo_gitcode_token IN (SELECT bot_gitcode_token FROM managed_repos WHERE bot_gitcode_token != '')
  );

-- 3. 同样逻辑清 repo_github_token（ensure_default 曾对 GitHub 仓库回填过）
UPDATE devops_agent_session d
SET d.repo_github_token = ''
WHERE d.repo_github_token != ''
  AND (
    d.repo_github_token = (SELECT value FROM meta WHERE `key`='global_bot_gitcode_token')
    OR d.repo_github_token IN (SELECT bot_gitcode_token FROM managed_repos WHERE bot_gitcode_token != '')
  );

-- 4. 验证：清完后残留的非空 session token（应为空或仅真·专属 token）
SELECT repo_key, agent_type, repo_gitcode_token != '' AS has_gc, repo_github_token != '' AS has_gh
FROM devops_agent_session
WHERE repo_gitcode_token != '' OR repo_github_token != '';
