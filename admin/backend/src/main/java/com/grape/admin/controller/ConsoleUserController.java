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
                    + "gitcode_api_token IS NOT NULL AND gitcode_api_token<>'' as has_gitcode_token, "
                    + "(last_active_at IS NOT NULL AND last_active_at >= NOW() - INTERVAL 30 MINUTE) as is_online "
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
                u.put("online", rs.getBoolean("is_online"));
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
                    + "gitcode_publish_repo, github_publish_repo, last_scene, "
                    + "(last_active_at IS NOT NULL AND last_active_at >= NOW() - INTERVAL 30 MINUTE) as is_online "
                    + "FROM user_accounts WHERE id=?", rs -> {
                info.put("id", rs.getInt("id"));
                info.put("username", rs.getString("username"));
                info.put("nickname", rs.getString("nickname"));
                info.put("gitcodeId", rs.getString("gitcode_id"));
                String lastActive = rs.getString("last_active_at");
                info.put("lastActiveAt", lastActive);
                info.put("online", rs.getBoolean("is_online"));
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

        // 用户活动统计（按状态机6个标准status值，对齐 observability）
        try {
            String issueFilter = " AND t.issue_id IS NOT NULL AND t.issue_id<>''";
            String baseJoin = " FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=?";
            String stdStatus = " AND t.status IN ('queued','dispatched','running','completed','failed','cancelled')";
            // taskCount 只统计6个标准status
            Long taskCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + issueFilter + stdStatus, Long.class, id);
            info.put("taskCount", taskCount != null ? taskCount : 0);
            Long issueTaskCount = taskCount;
            info.put("issueTaskCount", issueTaskCount != null ? issueTaskCount : 0);
            Long chatTaskCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + " AND (t.issue_id IS NULL OR t.issue_id='')", Long.class, id);
            info.put("chatTaskCount", chatTaskCount != null ? chatTaskCount : 0);
            Long issueCount = grapeJdbc.queryForObject(
                "SELECT COUNT(DISTINCT t.issue_id)" + baseJoin + issueFilter, Long.class, id);
            info.put("issueCount", issueCount != null ? issueCount : 0);
            // 6个标准状态计数
            Long queuedCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + issueFilter + " AND t.status='queued'", Long.class, id);
            info.put("queuedCount", queuedCount != null ? queuedCount : 0);
            Long dispatchedCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + issueFilter + " AND t.status='dispatched'", Long.class, id);
            info.put("dispatchedCount", dispatchedCount != null ? dispatchedCount : 0);
            Long runningCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + issueFilter + " AND t.status='running'", Long.class, id);
            info.put("runningCount", runningCount != null ? runningCount : 0);
            Long completedCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + issueFilter + " AND t.status='completed'", Long.class, id);
            info.put("completedCount", completedCount != null ? completedCount : 0);
            Long failedCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + issueFilter + " AND t.status='failed'", Long.class, id);
            info.put("failedCount", failedCount != null ? failedCount : 0);
            Long cancelledCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + issueFilter + " AND t.status='cancelled'", Long.class, id);
            info.put("cancelledCount", cancelledCount != null ? cancelledCount : 0);
            // 进行中 = dispatched + running
            long inProgress = (dispatchedCount != null ? dispatchedCount : 0) + (runningCount != null ? runningCount : 0);
            info.put("inProgressCount", inProgress);
            // 卡住 = running 且超22分钟
            Long stuckCount = grapeJdbc.queryForObject(
                "SELECT COUNT(*)" + baseJoin + issueFilter + " AND t.status='running' AND t.started_at IS NOT NULL AND t.started_at < DATE_SUB(NOW(), INTERVAL 22 MINUTE)", Long.class, id);
            info.put("stuckCount", stuckCount != null ? stuckCount : 0);
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
                "COUNT(CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' AND t.status IN ('queued','dispatched','running','completed','failed','cancelled') THEN 1 END) as task_count, " +
                "COUNT(DISTINCT CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' THEN t.issue_id END) as issue_count, " +
                "SUM(CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' AND t.status='queued' THEN 1 ELSE 0 END) as queued, " +
                "SUM(CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' AND t.status='dispatched' THEN 1 ELSE 0 END) as dispatched, " +
                "SUM(CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' AND t.status='running' THEN 1 ELSE 0 END) as running, " +
                "SUM(CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' AND t.status='completed' THEN 1 ELSE 0 END) as completed, " +
                "SUM(CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' AND t.status='failed' THEN 1 ELSE 0 END) as failed, " +
                "SUM(CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' AND t.status='cancelled' THEN 1 ELSE 0 END) as cancelled, " +
                "SUM(CASE WHEN t.issue_id IS NOT NULL AND t.issue_id<>'' AND t.status='running' AND t.started_at IS NOT NULL AND t.started_at < DATE_SUB(NOW(), INTERVAL 22 MINUTE) THEN 1 ELSE 0 END) as stuck " +
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
                    r.put("queued", rs2.getInt("queued"));
                    r.put("dispatched", rs2.getInt("dispatched"));
                    r.put("running", rs2.getInt("running"));
                    r.put("cancelled", rs2.getInt("cancelled"));
                    r.put("stuck", rs2.getInt("stuck"));
                    return r;
                }, id);
            info.put("repoMatrix", repoMatrix);
            info.put("repoCount", repoMatrix.size());
        } catch (Exception e) { /* ignore */ }

        // 最近活动（最近 5 条任务）
        try {
            List<Map<String, Object>> recentTasks = grapeJdbc.query(
                "SELECT t.id, t.repo_key, m.full_name, t.issue_id, t.status, t.exit_code, " +
                "t.agent_type, t.skill, t.started_at, t.completed_at, t.event_type, t.runtime_id, " +
                "ar.name as engine_name, ar.credential_id, ar.host_name, " +
                "rc.device_name, rc.last_ip " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner " +
                "LEFT JOIN agent_runtimes ar ON t.runtime_id = ar.id " +
                "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id " +
                "WHERE u.id=? AND t.issue_id IS NOT NULL AND t.issue_id<>'' " +
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
                    r.put("eventType", rs2.getString("event_type"));
                    // 引擎实例名（对齐 observability instance_name 逻辑）
                    r.put("engine", buildEngineName(rs2));
                    r.put("engineIp", rs2.getString("last_ip"));
                    // 耗时（秒）
                    String startedAt = rs2.getString("started_at");
                    String completedAt = rs2.getString("completed_at");
                    if (startedAt != null && completedAt != null) {
                        try {
                            String s = startedAt.replace("T", " ").length() > 19 ? startedAt.replace("T", " ").substring(0, 19) : startedAt.replace("T", " ");
                            String c = completedAt.replace("T", " ").length() > 19 ? completedAt.replace("T", " ").substring(0, 19) : completedAt.replace("T", " ");
                            java.sql.Timestamp start = java.sql.Timestamp.valueOf(s);
                            java.sql.Timestamp end = java.sql.Timestamp.valueOf(c);
                            long durationSec = (end.getTime() - start.getTime()) / 1000;
                            r.put("duration", durationSec + "s");
                        } catch (Exception ex) {
                            r.put("duration", "—");
                        }
                    } else {
                        r.put("duration", "—");
                    }
                    // 状态判断（直接用 status 字段，对齐 observability）
                    String taskStatus = rs2.getString("status");
                    if (taskStatus != null && !taskStatus.isEmpty()) {
                        r.put("status", taskStatus);
                    } else {
                        r.put("status", "unknown");
                    }
                    return r;
                }, id);
            info.put("recentTasks", recentTasks);
        } catch (Exception e) { /* ignore */ }

        // 进行中任务（最近 5 条）
        try {
            List<Map<String, Object>> recentPending = grapeJdbc.query(
                "SELECT t.id, t.repo_key, m.full_name, t.issue_id, t.status, t.exit_code, " +
                "t.agent_type, t.skill, t.started_at, t.completed_at, t.event_type, t.runtime_id, " +
                "ar.name as engine_name, ar.credential_id, ar.host_name, " +
                "rc.device_name, rc.last_ip " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner " +
                "LEFT JOIN agent_runtimes ar ON t.runtime_id = ar.id " +
                "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id " +
                "WHERE u.id=? AND t.status IN ('dispatched','running') " +
                "AND t.issue_id IS NOT NULL AND t.issue_id<>'' " +
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
                    r.put("eventType", rs2.getString("event_type"));
                    r.put("engine", buildEngineName(rs2));
                    r.put("engineIp", rs2.getString("last_ip"));
                    String sa = rs2.getString("started_at");
                    String ca = rs2.getString("completed_at");
                    r.put("duration", calcDuration(sa, ca));
                    r.put("status", "pending");
                    return r;
                }, id);
            info.put("recentPendingTasks", recentPending);
        } catch (Exception e) { /* ignore */ }

        // 最近失败任务（最近 5 条）
        try {
            List<Map<String, Object>> recentFailed = grapeJdbc.query(
                "SELECT t.id, t.repo_key, m.full_name, t.issue_id, t.status, t.exit_code, " +
                "t.agent_type, t.skill, t.started_at, t.completed_at, t.event_type, t.runtime_id, " +
                "ar.name as engine_name, ar.credential_id, ar.host_name, " +
                "rc.device_name, rc.last_ip " +
                "FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner " +
                "LEFT JOIN agent_runtimes ar ON t.runtime_id = ar.id " +
                "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id " +
                "WHERE u.id=? AND t.status='failed' " +
                "AND t.issue_id IS NOT NULL AND t.issue_id<>'' " +
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
                    r.put("eventType", rs2.getString("event_type"));
                    r.put("engine", buildEngineName(rs2));
                    r.put("engineIp", rs2.getString("last_ip"));
                    String sa = rs2.getString("started_at");
                    String ca = rs2.getString("completed_at");
                    r.put("duration", calcDuration(sa, ca));
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

        String joinSql = " FROM agent_tasks t" +
                " INNER JOIN managed_repos m ON t.repo_key = m.repo_key" +
                " INNER JOIN user_accounts u ON u.username = m.repo_owner" +
                " WHERE u.id=?" +
                " AND m.repo_key=?" +
                " AND t.issue_id IS NOT NULL AND t.issue_id<>''";
        Object[] baseParams = {id, repoKey};

        // Issue 总数
        long total = 0;
        try {
            total = grapeJdbc.queryForObject(
                "SELECT COUNT(DISTINCT t.issue_id)" + joinSql, Long.class, baseParams);
        } catch (Exception e) { /* ignore */ }

        // Issue 分页列表（GROUP BY issue_id，按最近活动时间倒序）
        List<Map<String, Object>> list = new ArrayList<>();
        try {
            String sql = "SELECT t.issue_id, " +
                    "COUNT(*) as task_count, " +
                    "SUM(CASE WHEN t.status='queued' THEN 1 ELSE 0 END) as queued, " +
                    "SUM(CASE WHEN t.status='dispatched' THEN 1 ELSE 0 END) as dispatched, " +
                    "SUM(CASE WHEN t.status='running' THEN 1 ELSE 0 END) as running, " +
                    "SUM(CASE WHEN t.status='completed' THEN 1 ELSE 0 END) as completed, " +
                    "SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) as failed, " +
                    "SUM(CASE WHEN t.status='cancelled' THEN 1 ELSE 0 END) as cancelled, " +
                    "SUM(CASE WHEN t.status='running' AND t.started_at IS NOT NULL AND t.started_at < DATE_SUB(NOW(), INTERVAL 22 MINUTE) THEN 1 ELSE 0 END) as stuck, " +
                    "MAX(t.started_at) as last_task_at " +
                    joinSql +
                    " GROUP BY t.issue_id" +
                    " ORDER BY last_task_at DESC LIMIT ? OFFSET ?";
            Object[] params = new Object[]{id, repoKey, size, page * size};
            grapeJdbc.query(sql, rs -> {
                Map<String, Object> r = new LinkedHashMap<>();
                r.put("issueId", rs.getString("issue_id"));
                r.put("taskCount", rs.getInt("task_count"));
                r.put("completed", rs.getInt("completed"));
                r.put("failed", rs.getInt("failed"));
                r.put("queued", rs.getInt("queued"));
                r.put("dispatched", rs.getInt("dispatched"));
                r.put("running", rs.getInt("running"));
                r.put("cancelled", rs.getInt("cancelled"));
                r.put("stuck", rs.getInt("stuck"));
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

    @GetMapping("/{id}/issue-timeline")
    public Map<String, Object> issueTimeline(@PathVariable int id,
                                             @RequestParam String repoKey,
                                             @RequestParam String issueId) {
        Map<String, Object> result = new LinkedHashMap<>();
        List<Map<String, Object>> events = new ArrayList<>();
        try {
            events = grapeJdbc.query(
                "SELECT e.seq, e.event_type, e.agent_type, e.skill, e.status, e.duration, e.created_at, " +
                "e.repo_key, t.id as task_id, t.runtime_id, " +
                "ar.name as engine_name, ar.credential_id, ar.host_name, " +
                "rc.device_name, rc.last_ip " +
                "FROM events e " +
                "LEFT JOIN agent_tasks t ON e.seq = t.event_seq AND e.repo_key = t.repo_key " +
                "LEFT JOIN agent_runtimes ar ON t.runtime_id = ar.id " +
                "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id " +
                "WHERE e.repo_key=? AND e.issue_id=? " +
                "ORDER BY e.created_at",
                (rs, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("seq", rs.getInt("seq"));
                    r.put("eventType", rs.getString("event_type"));
                    r.put("agentType", rs.getString("agent_type"));
                    r.put("skill", rs.getString("skill"));
                    r.put("status", rs.getString("status"));
                    r.put("duration", rs.getObject("duration"));
                    r.put("createdAt", rs.getString("created_at"));
                    r.put("repoKey", rs.getString("repo_key"));
                    r.put("taskId", rs.getString("task_id"));
                    r.put("engine", buildEngineName(rs));
                    r.put("engineIp", rs.getString("last_ip"));
                    return r;
                }, repoKey, issueId);
        } catch (Exception e) { /* ignore */ }
        result.put("issueId", issueId);
        result.put("events", events);
        return result;
    }

    private String calcDuration(String startedAt, String completedAt) {
        if (startedAt == null || completedAt == null) return "—";
        try {
            String s = startedAt.replace("T", " ");
            String c = completedAt.replace("T", " ");
            if (s.length() > 19) s = s.substring(0, 19);
            if (c.length() > 19) c = c.substring(0, 19);
            java.sql.Timestamp start = java.sql.Timestamp.valueOf(s);
            java.sql.Timestamp end = java.sql.Timestamp.valueOf(c);
            long durationSec = (end.getTime() - start.getTime()) / 1000;
            return durationSec + "s";
        } catch (Exception e) {
            return "—";
        }
    }

    /** 引擎实例名（对齐 grape observability instance_name 逻辑：{type}-{device_suffix}） */
    private String buildEngineName(java.sql.ResultSet rs) throws java.sql.SQLException {
        String engineName = rs.getString("engine_name");
        if (engineName == null || engineName.isEmpty()) return null;
        int credId = rs.getInt("credential_id");
        String deviceName = rs.getString("device_name");
        String hostName = rs.getString("host_name");
        // 本机引擎（credential_id=0）：优先用 host_name
        if (credId == 0) {
            deviceName = hostName != null ? hostName : "";
        }
        String et = engineName.trim().toLowerCase();
        String suffix = "";
        if (deviceName != null && !deviceName.isEmpty()) {
            // sanitize: 取最后一段（去掉域名等），转小写
            String[] parts = deviceName.split("\\.");
            suffix = parts[0].toLowerCase().replaceAll("[^a-z0-9-]", "");
        }
        if (suffix.isEmpty()) {
            suffix = "cred" + credId;
        }
        return et + "-" + suffix;
    }
}
