package com.grape.admin.controller;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

import java.util.*;

/**
 * Grape 控制台用户管理（user_accounts 表，托管仓库的登录用户）
 */
@RestController
@RequestMapping("/api/admin/console-users")
public class ConsoleUserController {

    private final JdbcTemplate grapeJdbc;

    public ConsoleUserController(@Qualifier("grapeJdbcTemplate") JdbcTemplate grapeJdbc) {
        this.grapeJdbc = grapeJdbc;
    }

    @GetMapping
    public Map<String, Object> list(@RequestParam(required = false, defaultValue = "0") int page,
                                    @RequestParam(required = false, defaultValue = "10") int size,
                                    @RequestParam(required = false) String keyword,
                                    @RequestParam(required = false) String source) {
        Map<String, Object> result = new LinkedHashMap<>();

        StringBuilder where = new StringBuilder(" WHERE 1=1");
        List<Object> params = new ArrayList<>();
        if (keyword != null && !keyword.isBlank()) {
            where.append(" AND (username LIKE ? OR nickname LIKE ?)");
            params.add("%" + keyword + "%");
            params.add("%" + keyword + "%");
        }
        if (source != null && !source.isBlank()) {
            if ("gitcode".equals(source)) {
                where.append(" AND gitcode_id IS NOT NULL AND gitcode_id<>''");
            } else if ("other".equals(source)) {
                where.append(" AND (gitcode_id IS NULL OR gitcode_id='')");
            } else if ("online".equals(source)) {
                where.append(" AND last_active_at IS NOT NULL AND last_active_at >= NOW() - INTERVAL 30 MINUTE");
            } else if ("offline".equals(source)) {
                where.append(" AND (last_active_at IS NULL OR last_active_at < NOW() - INTERVAL 30 MINUTE)");
            }
        }

        long total = 0;
        try {
            total = grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts" + where, Long.class, params.toArray());
        } catch (Exception e) { /* ignore */ }

        List<Map<String, Object>> list = new ArrayList<>();
        try {
            String sql = "SELECT id, username, nickname, gitcode_id, created_at, last_active_at, "
                    + "huawei_cloud_ak IS NOT NULL AND huawei_cloud_ak<>'' as has_huawei_ak, "
                    + "feishu_app_id IS NOT NULL AND feishu_app_id<>'' as has_feishu, "
                    + "gitcode_api_token IS NOT NULL AND gitcode_api_token<>'' as has_gitcode_token "
                    + "FROM user_accounts" + where + " ORDER BY last_active_at DESC, id DESC LIMIT ? OFFSET ?";
            params.add(size);
            params.add(page * size);
            grapeJdbc.query(sql, rs -> {
                Map<String, Object> u = new LinkedHashMap<>();
                u.put("id", rs.getInt("id"));
                u.put("username", rs.getString("username"));
                u.put("nickname", rs.getString("nickname"));
                u.put("gitcodeId", rs.getString("gitcode_id"));
                u.put("createdAt", rs.getString("created_at"));
                String lastActive = rs.getString("last_active_at");
                u.put("lastActiveAt", lastActive);
                u.put("online", isOnline(lastActive));
                u.put("hasHuaweiAk", rs.getBoolean("has_huawei_ak"));
                u.put("hasFeishu", rs.getBoolean("has_feishu"));
                u.put("hasGitcodeToken", rs.getBoolean("has_gitcode_token"));
                list.add(u);
            }, params.toArray());
        } catch (Exception e) { /* ignore */ }

        // 全量统计（不受筛选影响）
        Map<String, Object> stats = new LinkedHashMap<>();
        try {
            stats.put("total", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts", Long.class));
            stats.put("online", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE last_active_at IS NOT NULL AND last_active_at >= NOW() - INTERVAL 30 MINUTE", Long.class));
            stats.put("offline", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE last_active_at IS NULL OR last_active_at < NOW() - INTERVAL 30 MINUTE", Long.class));
            stats.put("gitcodeLogin", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE gitcode_id IS NOT NULL AND gitcode_id<>''", Long.class));
            stats.put("other", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE gitcode_id IS NULL OR gitcode_id=''", Long.class));
        } catch (Exception e) { /* ignore */ }

        result.put("content", list);
        result.put("totalElements", total);
        result.put("totalPages", (total + size - 1) / size);
        result.put("page", page);
        result.put("size", size);
        result.put("stats", stats);
        return result;
    }

    @GetMapping("/{id}")
    public Map<String, Object> detail(@PathVariable int id) {
        Map<String, Object> info = new LinkedHashMap<>();
        try {
            grapeJdbc.query("SELECT id, username, nickname, gitcode_id, created_at, last_active_at, "
                    + "huawei_cloud_ak IS NOT NULL AND huawei_cloud_ak<>'' as has_huawei_ak, "
                    + "huawei_cloud_sk IS NOT NULL AND huawei_cloud_sk<>'' as has_huawei_sk, "
                    + "feishu_app_id IS NOT NULL AND feishu_app_id<>'' as has_feishu, "
                    + "feishu_app_secret IS NOT NULL AND feishu_app_secret<>'' as has_feishu_secret, "
                    + "gitcode_api_token IS NOT NULL AND gitcode_api_token<>'' as has_gitcode_token, "
                    + "CODEARTS_CLI_AK IS NOT NULL AND CODEARTS_CLI_AK<>'' as has_codearts_ak, "
                    + "CODEARTS_CLI_SK IS NOT NULL AND CODEARTS_CLI_SK<>'' as has_codearts_sk, "
                    + "gitcode_publish_repo, github_publish_repo, last_scene "
                    + "FROM user_accounts WHERE id=?", rs -> {
                info.put("id", rs.getInt("id"));
                info.put("username", rs.getString("username"));
                info.put("nickname", rs.getString("nickname"));
                info.put("gitcodeId", rs.getString("gitcode_id"));
                String lastActive = rs.getString("last_active_at");
                info.put("lastActiveAt", lastActive);
                info.put("online", isOnline(lastActive));
                info.put("createdAt", rs.getString("created_at"));
                info.put("hasHuaweiAk", rs.getBoolean("has_huawei_ak"));
                info.put("hasHuaweiSk", rs.getBoolean("has_huawei_sk"));
                info.put("hasFeishu", rs.getBoolean("has_feishu"));
                info.put("hasFeishuSecret", rs.getBoolean("has_feishu_secret"));
                info.put("hasGitcodeToken", rs.getBoolean("has_gitcode_token"));
                info.put("hasCodeartsAk", rs.getBoolean("has_codearts_ak"));
                info.put("hasCodeartsSk", rs.getBoolean("has_codearts_sk"));
                info.put("gitcodePublishRepo", rs.getString("gitcode_publish_repo"));
                info.put("githubPublishRepo", rs.getString("github_publish_repo"));
                info.put("lastScene", rs.getString("last_scene"));
            }, id);
        } catch (Exception e) { /* ignore */ }

        // 用户活动统计（通过 repo_owner 关联仓库再关联任务，因为 agent_tasks.user_id 大量为 0）
        try {
            Long taskCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=?", Long.class, id);
            info.put("taskCount", taskCount != null ? taskCount : 0);
            Long issueTaskCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? AND t.issue_id IS NOT NULL AND t.issue_id<>''", Long.class, id);
            info.put("issueTaskCount", issueTaskCount != null ? issueTaskCount : 0);
            Long chatTaskCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? AND (t.issue_id IS NULL OR t.issue_id='')", Long.class, id);
            info.put("chatTaskCount", chatTaskCount != null ? chatTaskCount : 0);
            Long issueCount = grapeJdbc.queryForObject(
                "SELECT COUNT(DISTINCT t.issue_id) FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? AND t.issue_id IS NOT NULL AND t.issue_id<>''", Long.class, id);
            info.put("issueCount", issueCount != null ? issueCount : 0);
            Long completedCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? AND t.exit_code=0 AND t.completed_at IS NOT NULL", Long.class, id);
            info.put("completedCount", completedCount != null ? completedCount : 0);
            Long failedCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? AND t.exit_code>0", Long.class, id);
            info.put("failedCount", failedCount != null ? failedCount : 0);
            Long pendingCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? AND (t.exit_code IS NULL OR t.completed_at IS NULL)", Long.class, id);
            info.put("pendingCount", pendingCount != null ? pendingCount : 0);
            if (taskCount != null && taskCount > 0 && completedCount != null) {
                info.put("successRate", Math.round(completedCount * 1000.0 / taskCount) / 10.0 + "%");
            } else {
                info.put("successRate", "—");
            }
        } catch (Exception e) { /* ignore */ }

        // 仓库 × Issue 矩阵（Issue 分析 tab 用）
        try {
            List<Map<String, Object>> repoMatrix = grapeJdbc.query(
                "SELECT m.full_name, m.repo_key, " +
                "COUNT(*) as task_count, " +
                "COUNT(DISTINCT CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' THEN t.issue_id END) as issue_count, " +
                "SUM(CASE WHEN t.exit_code=0 AND t.completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed, " +
                "SUM(CASE WHEN t.exit_code>0 THEN 1 ELSE 0 END) as failed, " +
                "SUM(CASE WHEN (t.exit_code IS NULL OR t.completed_at IS NULL) AND t.issue_id IS NOT NULL AND t.issue_id<>'' THEN 1 ELSE 0 END) as pending " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? " +
                "GROUP BY m.full_name, m.repo_key ORDER BY task_count DESC",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("fullName", rs2.getString("full_name"));
                    r.put("repoKey", rs2.getString("repo_key"));
                    r.put("taskCount", rs2.getInt("task_count"));
                    r.put("issueCount", rs2.getInt("issue_count"));
                    r.put("completed", rs2.getInt("completed"));
                    r.put("failed", rs2.getInt("failed"));
                    r.put("pending", rs2.getInt("pending"));
                    return r;
                }, id);
            info.put("repoMatrix", repoMatrix);
            info.put("repoCount", repoMatrix.size());

            // 每个 Issue 的明细
            List<Map<String, Object>> issueDetails = grapeJdbc.query(
                "SELECT m.repo_key, m.full_name, t.issue_id, " +
                "COUNT(*) as task_count, " +
                "SUM(CASE WHEN t.exit_code=0 AND t.completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed, " +
                "SUM(CASE WHEN t.exit_code>0 THEN 1 ELSE 0 END) as failed, " +
                "SUM(CASE WHEN t.exit_code IS NULL OR t.completed_at IS NULL THEN 1 ELSE 0 END) as pending " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? " +
                "AND t.issue_id IS NOT NULL AND t.issue_id<>'' " +
                "GROUP BY m.repo_key, m.full_name, t.issue_id ORDER BY m.full_name, t.issue_id",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("repoKey", rs2.getString("repo_key"));
                    r.put("fullName", rs2.getString("full_name"));
                    r.put("issueId", rs2.getString("issue_id"));
                    r.put("taskCount", rs2.getInt("task_count"));
                    r.put("completed", rs2.getInt("completed"));
                    r.put("failed", rs2.getInt("failed"));
                    r.put("pending", rs2.getInt("pending"));
                    return r;
                }, id);
            info.put("issueDetails", issueDetails);
        } catch (Exception e) { /* ignore */ }

        // 最近活动（最近 5 条任务）
        try {
            List<Map<String, Object>> recentTasks = grapeJdbc.query(
                "SELECT t.id, t.repo_key, m.full_name, t.issue_id, t.status, t.exit_code, " +
                "t.agent_type, t.skill, t.started_at, t.completed_at " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? " +
                "ORDER BY t.started_at DESC LIMIT 5",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("id", rs2.getString("id"));
                    String fullName = rs2.getString("full_name");
                    r.put("repoName", fullName != null ? fullName : rs2.getString("repo_key"));
                    r.put("issueId", rs2.getString("issue_id"));
                    r.put("exitCode", rs2.getObject("exit_code"));
                    r.put("agentType", rs2.getString("agent_type"));
                    r.put("skill", rs2.getString("skill"));
                    r.put("startedAt", rs2.getString("started_at"));
                    r.put("completedAt", rs2.getString("completed_at"));
                    // 状态判断
                    Object exitCode = rs2.getObject("exit_code");
                    String completedAt = rs2.getString("completed_at");
                    if (exitCode != null && ((Integer) exitCode) == 0 && completedAt != null) {
                        r.put("status", "completed");
                    } else if (exitCode != null && ((Integer) exitCode) > 0) {
                        r.put("status", "failed");
                    } else {
                        r.put("status", "pending");
                    }
                    return r;
                }, id);
            info.put("recentTasks", recentTasks);
        } catch (Exception e) { /* ignore */ }

        // 进行中任务（最近 5 条）
        try {
            List<Map<String, Object>> recentPending = grapeJdbc.query(
                "SELECT t.id, t.repo_key, m.full_name, t.issue_id, t.status, t.exit_code, " +
                "t.agent_type, t.skill, t.started_at, t.completed_at " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? " +
                "AND (t.exit_code IS NULL OR t.completed_at IS NULL) " +
                "ORDER BY t.started_at DESC LIMIT 5",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("id", rs2.getString("id"));
                    String fullName = rs2.getString("full_name");
                    r.put("repoName", fullName != null ? fullName : rs2.getString("repo_key"));
                    r.put("issueId", rs2.getString("issue_id"));
                    r.put("exitCode", rs2.getObject("exit_code"));
                    r.put("agentType", rs2.getString("agent_type"));
                    r.put("skill", rs2.getString("skill"));
                    r.put("startedAt", rs2.getString("started_at"));
                    r.put("completedAt", rs2.getString("completed_at"));
                    r.put("status", "pending");
                    return r;
                }, id);
            info.put("recentPendingTasks", recentPending);
        } catch (Exception e) { /* ignore */ }

        // 最近失败任务（最近 5 条）
        try {
            List<Map<String, Object>> recentFailed = grapeJdbc.query(
                "SELECT t.id, t.repo_key, m.full_name, t.issue_id, t.status, t.exit_code, " +
                "t.agent_type, t.skill, t.started_at, t.completed_at " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? " +
                "AND t.exit_code>0 " +
                "ORDER BY t.started_at DESC LIMIT 5",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("id", rs2.getString("id"));
                    String fullName = rs2.getString("full_name");
                    r.put("repoName", fullName != null ? fullName : rs2.getString("repo_key"));
                    r.put("issueId", rs2.getString("issue_id"));
                    r.put("exitCode", rs2.getObject("exit_code"));
                    r.put("agentType", rs2.getString("agent_type"));
                    r.put("skill", rs2.getString("skill"));
                    r.put("startedAt", rs2.getString("started_at"));
                    r.put("completedAt", rs2.getString("completed_at"));
                    r.put("status", "failed");
                    return r;
                }, id);
            info.put("recentFailedTasks", recentFailed);
        } catch (Exception e) { /* ignore */ }

        return info;
    }

    @GetMapping("/{id}/repo-issues")
    public Map<String, Object> repoIssues(@PathVariable int id,
                                          @RequestParam String repoKey,
                                          @RequestParam(defaultValue = "0") int page,
                                          @RequestParam(defaultValue = "5") int size) {
        Map<String, Object> result = new LinkedHashMap<>();
        String fullRepoKey = "gitcode:" + repoKey;
        String cleanRepoKey = repoKey.replace("gitcode:", "");

        String joinSql = " FROM agent_tasks t" +
                " INNER JOIN managed_repos m ON t.repo_key = m.repo_key" +
                " INNER JOIN user_accounts u ON u.username = m.repo_owner" +
                " WHERE u.id=?" +
                " AND (m.repo_key=? OR m.repo_key=? OR m.repo_key=?)" +
                " AND t.issue_id IS NOT NULL AND t.issue_id<>''";
        Object[] baseParams = {id, repoKey, fullRepoKey, cleanRepoKey};

        // Issue 总数
        long total = 0;
        try {
            total = grapeJdbc.queryForObject(
                "SELECT COUNT(DISTINCT CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' THEN t.issue_id END)" + joinSql, Long.class, baseParams);
        } catch (Exception e) { /* ignore */ }

        // Issue 分页列表（GROUP BY issue_id，按最近活动时间倒序）
        List<Map<String, Object>> list = new ArrayList<>();
        try {
            String sql = "SELECT t.issue_id, " +
                    "COUNT(*) as task_count, " +
                    "SUM(CASE WHEN t.exit_code=0 AND t.completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed, " +
                    "SUM(CASE WHEN t.exit_code>0 THEN 1 ELSE 0 END) as failed, " +
                    "SUM(CASE WHEN t.exit_code IS NULL OR t.completed_at IS NULL THEN 1 ELSE 0 END) as pending, " +
                    "MAX(t.started_at) as last_task_at " +
                    joinSql +
                    " GROUP BY t.issue_id" +
                    " ORDER BY last_task_at DESC LIMIT ? OFFSET ?";
            Object[] params = new Object[]{id, repoKey, fullRepoKey, cleanRepoKey, size, page * size};
            grapeJdbc.query(sql, rs -> {
                Map<String, Object> r = new LinkedHashMap<>();
                r.put("issueId", rs.getString("issue_id"));
                r.put("taskCount", rs.getInt("task_count"));
                r.put("completed", rs.getInt("completed"));
                r.put("failed", rs.getInt("failed"));
                r.put("pending", rs.getInt("pending"));
                r.put("lastTaskAt", rs.getString("last_task_at"));
                list.add(r);
            }, params);
        } catch (Exception e) { /* ignore */ }

        result.put("content", list);
        result.put("totalElements", total);
        result.put("totalPages", (total + size - 1) / size);
        result.put("page", page);
        result.put("size", size);
        return result;
    }

    @PostMapping("/{id}/reset-token")
    public Map<String, Object> resetToken(@PathVariable int id) {
        Map<String, Object> result = new LinkedHashMap<>();
        try {
            grapeJdbc.update("UPDATE user_accounts SET token_version=token_version+1 WHERE id=?", id);
            grapeJdbc.update("UPDATE user_accounts SET last_active_at=NULL WHERE id=?", id);
            result.put("success", true);
            result.put("message", "已强制下线，用户需要重新登录");
        } catch (Exception e) {
            result.put("success", false);
            result.put("message", "操作失败: " + e.getMessage());
        }
        return result;
    }

    private boolean isOnline(String lastActiveAt) {
        if (lastActiveAt == null || lastActiveAt.isBlank()) return false;
        try {
            String ts = lastActiveAt.length() > 19 ? lastActiveAt.substring(0, 19) : lastActiveAt;
            ts = ts.replace(' ', 'T');
            java.time.LocalDateTime lat = java.time.LocalDateTime.parse(ts);
            return java.time.Duration.between(lat, java.time.LocalDateTime.now()).toMinutes() < 30;
        } catch (Exception e) { return false; }
    }
}
