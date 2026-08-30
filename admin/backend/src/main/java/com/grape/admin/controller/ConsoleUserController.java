package com.grape.admin.controller;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
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

    private static final Logger log = LoggerFactory.getLogger(ConsoleUserController.class);

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
        } catch (Exception e) { log.warn("console-users count failed: {}", e.getMessage()); }

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
        } catch (Exception e) { log.warn("console-users query failed: {}", e.getMessage()); }

        // 全量统计（不受筛选影响）
        Map<String, Object> stats = new LinkedHashMap<>();
        try {
            stats.put("total", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts", Long.class));
            stats.put("online", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE last_active_at IS NOT NULL AND last_active_at >= NOW() - INTERVAL 30 MINUTE", Long.class));
            stats.put("offline", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE last_active_at IS NULL OR last_active_at < NOW() - INTERVAL 30 MINUTE", Long.class));
            stats.put("gitcodeLogin", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE gitcode_id IS NOT NULL AND gitcode_id<>''", Long.class));
            stats.put("other", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE gitcode_id IS NULL OR gitcode_id=''", Long.class));
        } catch (Exception e) { log.warn("console-users stats failed: {}", e.getMessage()); }

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

        // 仓库 × Issue 矩阵（Issue 分析 tab 用）
        try {
            List<Map<String, Object>> repoMatrix = grapeJdbc.query(
                "SELECT m.full_name, m.repo_key " +
                "FROM managed_repos m " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=? " +
                "ORDER BY m.full_name",
                (rs2, rowNum) -> {
                    Map<String, Object> r = new LinkedHashMap<>();
                    r.put("fullName", rs2.getString("full_name"));
                    r.put("repoKey", rs2.getString("repo_key"));
                    return r;
                }, id);
            info.put("repoMatrix", repoMatrix);
        } catch (Exception e) { /* ignore */ }


        return info;
    }

    @GetMapping("/{id}/repo-issues")
    public Map<String, Object> repoIssues(@PathVariable int id,
                                          @RequestParam(required = false) String repoKey,
                                          @RequestParam(defaultValue = "0") int page,
                                          @RequestParam(defaultValue = "5") int size) {
        Map<String, Object> result = new LinkedHashMap<>();

        // 1. 查该用户关联的所有仓库 repo_key（对齐健康中心，加用户限制）
        List<String> userRepoKeys = new ArrayList<>();
        try {
            userRepoKeys = grapeJdbc.queryForList(
                "SELECT DISTINCT t.repo_key FROM agent_tasks t " +
                "INNER JOIN managed_repos m ON t.repo_key = m.repo_key " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner " +
                "WHERE u.id=? AND t.issue_id IS NOT NULL AND t.issue_id<>''", String.class, id);
        } catch (Exception e) { /* ignore */ }

        if (userRepoKeys.isEmpty()) {
            result.put("content", List.of());
            result.put("totalElements", 0);
            result.put("totalPages", 0);
            result.put("page", page);
            result.put("size", size);
            return result;
        }

        // 2. 查 events（对齐健康中心：直接查 events 表，Java 里分组）
        StringBuilder where = new StringBuilder(" WHERE issue_id IS NOT NULL AND issue_id<>''");
        List<Object> params = new ArrayList<>();
        // 限制用户关联的仓库
        if (userRepoKeys.size() == 1) {
            where.append(" AND repo_key=?");
            params.add(userRepoKeys.get(0));
        } else {
            where.append(" AND repo_key IN (");
            for (int i = 0; i < userRepoKeys.size(); i++) {
                if (i > 0) where.append(",");
                where.append("?");
                params.add(userRepoKeys.get(i));
            }
            where.append(")");
        }
        // 仓库筛选
        if (repoKey != null && !repoKey.isEmpty()) {
            where.append(" AND repo_key=?");
            params.add(repoKey);
        }

        List<Map<String, Object>> rows;
        try {
            rows = grapeJdbc.queryForList(
                "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, repo_key"
                + " FROM events" + where + " ORDER BY created_at", params.toArray());
        } catch (Exception e) {
            rows = List.of();
        }

        // 3. 按 repo_key + issue_id 组合分组（issue_id不唯一，不同仓库有相同编号）
        Map<String, List<Map<String, Object>>> group = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            String groupKey = row.get("repo_key") + "|" + row.get("issue_id");
            group.computeIfAbsent(groupKey, k -> new ArrayList<>()).add(row);
        }

        // 3.5 批量查 issue_opened 事件的 payload 提取标题（避免每行带 payload TEXT 大字段）
        com.fasterxml.jackson.databind.ObjectMapper om = new com.fasterxml.jackson.databind.ObjectMapper();
        Map<String, String> titleByKey = new java.util.HashMap<>();
        if (!group.isEmpty()) {
            try {
                StringBuilder titleSql = new StringBuilder(
                    "SELECT issue_id, repo_key, payload FROM events WHERE event_type='issue_opened' AND (");
                List<Object> titleParams = new ArrayList<>();
                boolean first = true;
                for (String gk : group.keySet()) {
                    String[] parts = gk.split("\\|", 2);
                    if (parts.length < 2) continue;
                    if (!first) titleSql.append(" OR ");
                    titleSql.append("(issue_id=? AND repo_key=?)");
                    titleParams.add(parts[1]);
                    titleParams.add(parts[0]);
                    first = false;
                }
                titleSql.append(")");
                grapeJdbc.query(titleSql.toString(), rs -> {
                    String key = rs.getString("repo_key") + "|" + rs.getString("issue_id");
                    if (!titleByKey.containsKey(key)) {
                        String t = extractIssueTitle(rs.getString("payload"), om);
                        if (!t.isEmpty()) titleByKey.put(key, t);
                    }
                }, titleParams.toArray());
            } catch (Exception e) { /* ignore */ }
        }

        // 4. 构建结果（对齐健康中心）
        List<Map<String, Object>> allIssues = new ArrayList<>();
        for (Map.Entry<String, List<Map<String, Object>>> e : group.entrySet()) {
            List<Map<String, Object>> evs = e.getValue();
            Map<String, Object> g = new LinkedHashMap<>();
            g.put("issueId", evs.get(0).get("issue_id"));
            // repoName 从 repo_key 提取
            String rk = (String) evs.get(0).get("repo_key");
            String repoName = rk != null ? rk.replaceFirst("^gitcode:", "") : "";
            g.put("repoName", repoName);
            g.put("repoKey", rk);
            // 从批量查的 title 缓存取标题
            g.put("issueTitle", titleByKey.getOrDefault(e.getKey(), ""));
            g.put("eventCount", evs.size());
            g.put("lastTaskAt", evs.get(evs.size() - 1).get("created_at"));
            // 统计失败数
            int failed = 0;
            for (Map<String, Object> ev : evs) {
                if ("failed".equals(ev.get("status"))) failed++;
            }
            g.put("failed", failed);
            allIssues.add(g);
        }

        // 5. 分页（按 lastTaskAt 倒序后分页）
        allIssues.sort((a, b) -> {
            String aT = (String) a.get("lastTaskAt");
            String bT = (String) b.get("lastTaskAt");
            if (aT == null) aT = "";
            if (bT == null) bT = "";
            return bT.compareTo(aT);
        });
        int total = allIssues.size();
        int from = Math.min(page * size, total);
        int to = Math.min(from + size, total);
        List<Map<String, Object>> pagedIssues = allIssues.subList(from, to);

        result.put("content", pagedIssues);
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
                                             @RequestParam(required = false) String repoKey,
                                             @RequestParam String issueId) {
        Map<String, Object> result = new LinkedHashMap<>();
        List<Map<String, Object>> events = new ArrayList<>();
        try {
            // 1. 限制到当前用户关联的仓库（安全：admin 不能查非自己用户的 issue）
            List<String> userRepoKeys = grapeJdbc.queryForList(
                "SELECT DISTINCT m.repo_key FROM managed_repos m " +
                "INNER JOIN user_accounts u ON u.username = m.repo_owner WHERE u.id=?", String.class, id);
            if (userRepoKeys.isEmpty()) {
                result.put("issueId", issueId);
                result.put("events", events);
                return result;
            }

            // 2. 构建 WHERE：issueId + 用户仓库限制 + 可选 repoKey 筛选
            StringBuilder where = new StringBuilder(" WHERE e.issue_id=?");
            List<Object> params = new ArrayList<>();
            params.add(issueId);
            if (userRepoKeys.size() == 1) {
                where.append(" AND e.repo_key=?");
                params.add(userRepoKeys.get(0));
            } else {
                where.append(" AND e.repo_key IN (");
                for (int i = 0; i < userRepoKeys.size(); i++) {
                    if (i > 0) where.append(",");
                    where.append("?");
                    params.add(userRepoKeys.get(i));
                }
                where.append(")");
            }
            if (repoKey != null && !repoKey.isEmpty()) {
                where.append(" AND e.repo_key=?");
                params.add(repoKey);
            }

            // 3. LEFT JOIN 一次性查出 events + task_id + engine 信息（对齐 RuntimeController.issueEvents）
            events = grapeJdbc.query(
                "SELECT e.seq, e.event_type, e.agent_type, e.skill, e.status, e.duration, e.created_at, " +
                "e.repo_key, " +
                "t.id as task_id, " +
                "ar.name as engine_name, " +
                "COALESCE(rc.device_name, '本机直连') as engine_device, " +
                "COALESCE(rc.last_ip, '127.0.0.1') as last_ip " +
                "FROM events e " +
                "LEFT JOIN agent_tasks t ON t.event_seq = e.seq AND t.repo_key = e.repo_key " +
                "LEFT JOIN agent_runtimes ar ON t.runtime_id = ar.id " +
                "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id " +
                where + " ORDER BY e.created_at",
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
                }, params.toArray());
        } catch (Exception e) { /* ignore */ }
        result.put("issueId", issueId);
        result.put("events", events);
        return result;
    }

    private String extractIssueTitle(Object payload, com.fasterxml.jackson.databind.ObjectMapper om) {
        if (payload == null) return "";
        try {
            String json = payload.toString();
            com.fasterxml.jackson.databind.JsonNode root = om.readTree(json);
            com.fasterxml.jackson.databind.JsonNode attrs = root.path("object_attributes");
            if (!attrs.isMissingNode()) {
                String t = attrs.path("title").asText("");
                if (!t.isEmpty()) return t;
            }
        } catch (Exception e) { /* ignore */ }
        return "";
    }

    /** 引擎实例名（对齐健康中心格式: name-device(ip)） */
    private String buildEngineName(java.sql.ResultSet rs) throws java.sql.SQLException {
        String engineName = rs.getString("engine_name");
        if (engineName == null || engineName.isEmpty()) return null;
        String device = rs.getString("engine_device");
        String ip = rs.getString("last_ip");
        if (device == null || device.isEmpty()) device = "本机直连";
        if (ip == null || ip.isEmpty()) ip = "127.0.0.1";
        return engineName + "-" + device + "(" + ip + ")";
    }
}
