package com.grape.admin.controller;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.grape.admin.service.HealthService;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.*;

/**
 * 健康中心：三维度（任务/设备/基础设施）KPI + 全量列表 + 告警汇总。
 * 直读共享库（grape_test_db），聚合方式对齐主服务 observability 页面。
 *
 * 设计变更（2026-08-27）：任务/设备 tab 展示全量数据（不只是异常），
 * 异常项（failed/卡住任务 + 离线设备 + DB 不可达）统一汇聚到告警中心。
 */
@RestController
@RequestMapping("/api/admin")
public class HealthCenterController {

    private final JdbcTemplate grapeJdbc;
    private final HealthService healthService;

    public HealthCenterController(@Qualifier("grapeJdbcTemplate") JdbcTemplate grapeJdbc,
                                  HealthService healthService) {
        this.grapeJdbc = grapeJdbc;
        this.healthService = healthService;
    }

    /** 设备概览：Worker 卡片 + Runtime 引擎表格（对齐主服务概览 tab） */
    @GetMapping("/health-center/overview")
    public Map<String, Object> overview() {
        Map<String, Object> result = new LinkedHashMap<>();

        // 1. Workers（凭据 = 远端 worker + 本机虚拟 worker）
        List<Map<String, Object>> workerList = new ArrayList<>();
        java.util.concurrent.atomic.AtomicInteger workerOnline = new java.util.concurrent.atomic.AtomicInteger(0);

        // 设备监控只显示远端 worker（本机作为运营管理平台不列入设备列表）

        // 远端 worker（runtime_credentials 表，排除 name='本机' 虚拟记录）
        try {
            grapeJdbc.query("SELECT id, name, device_name, status, last_seen_at, last_ip, " +
                    "cpu_percent, memory_percent, disk_percent, load_avg, owner_name " +
                    "FROM runtime_credentials WHERE name != '本机' ORDER BY id", rs -> {
                Map<String, Object> w = new LinkedHashMap<>();
                int wid = rs.getInt("id");
                String wStatus = rs.getString("status");
                String wLastSeen = rs.getString("last_seen_at");
                w.put("id", wid);
                w.put("name", rs.getString("name"));
                w.put("deviceName", rs.getString("device_name"));
                w.put("status", wStatus);
                w.put("health", healthService.deviceHealth(wStatus, wLastSeen));
                w.put("lastSeenAt", wLastSeen);
                w.put("lastIp", rs.getString("last_ip"));
                w.put("cpuPercent", rs.getDouble("cpu_percent"));
                w.put("memoryPercent", rs.getDouble("memory_percent"));
                w.put("diskPercent", rs.getDouble("disk_percent"));
                w.put("loadAvg", rs.getString("load_avg"));
                w.put("ownerName", rs.getString("owner_name"));
                if ("online".equals(w.get("health"))) workerOnline.incrementAndGet();
                workerList.add(w);
            });
        } catch (Exception e) { /* runtime_credentials 表可能不存在 */ }

        Map<String, Object> workers = new LinkedHashMap<>();
        workers.put("total", workerList.size());
        workers.put("online", workerOnline.get());
        workers.put("offline", workerList.size() - workerOnline.get());
        workers.put("list", workerList);
        result.put("workers", workers);

        // 2. Runtimes（引擎列表，关联 worker + running 任务数）
        // 先查每个 runtime 的 running 任务数 + 平均耗时
        Map<Integer, Integer> runningCounts = new HashMap<>();
        try {
            grapeJdbc.query("SELECT runtime_id, COUNT(*) FROM agent_tasks WHERE status='running' GROUP BY runtime_id",
                    (org.springframework.jdbc.core.RowCallbackHandler) rs -> runningCounts.put(rs.getInt(1), rs.getInt(2)));
        } catch (Exception e) { /* ignore */ }

        Map<Integer, Double> avgDurations = new HashMap<>();
        try {
            grapeJdbc.query("SELECT runtime_id, AVG(TIMESTAMPDIFF(SECOND, started_at, completed_at)) as avg_sec "
                    + "FROM agent_tasks WHERE status IN ('completed','failed') AND runtime_id > 0 "
                    + "AND started_at IS NOT NULL AND completed_at IS NOT NULL GROUP BY runtime_id",
                    (org.springframework.jdbc.core.RowCallbackHandler) rs -> avgDurations.put(rs.getInt(1), Math.round(rs.getDouble(2) * 10) / 10.0));
        } catch (Exception e) { /* ignore */ }

        List<Map<String, Object>> runtimeList = new ArrayList<>();
        java.util.concurrent.atomic.AtomicInteger rtOnline = new java.util.concurrent.atomic.AtomicInteger(0);
        try {
            grapeJdbc.query("SELECT id, name, scope, status, healthy, busy_slots, max_concurrent, " +
                    "version, last_seen_at, cli_path, credential_id " +
                    "FROM agent_runtimes ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END, last_seen_at DESC",
                    rs -> {
                        Map<String, Object> r = new LinkedHashMap<>();
                        int rid = rs.getInt("id");
                        int cid = rs.getInt("credential_id");
                        String status = rs.getString("status");
                        String lastSeen = rs.getString("last_seen_at");
                        String health = healthService.engineHealth(status, lastSeen);
                        // 本机直连引擎（credential_id=0）始终算在线 + 当前时间作为心跳
                        boolean isLocal = cid == 0;
                        if (isLocal) {
                            health = "online";
                            if (lastSeen == null || lastSeen.isEmpty()) {
                                lastSeen = java.time.OffsetDateTime.now().toString();
                            }
                        }

                        r.put("id", rid);
                        r.put("name", rs.getString("name"));
                        r.put("status", status);
                        r.put("health", health);
                        r.put("mode", isLocal ? "local" : "remote");
                        r.put("version", rs.getString("version"));
                        r.put("busySlots", rs.getInt("busy_slots"));
                        r.put("maxConcurrent", rs.getInt("max_concurrent"));
                        r.put("running", runningCounts.getOrDefault(rid, 0));
                        r.put("avgDuration", avgDurations.getOrDefault(rid, 0.0));
                        r.put("lastSeenAt", lastSeen);
                        r.put("credentialId", cid);
                        r.put("scope", rs.getString("scope"));
                        runtimeList.add(r);
                        if ("online".equals(health)) rtOnline.incrementAndGet();
                    });
        } catch (Exception e) { /* ignore */ }

        Map<String, Object> runtimes = new LinkedHashMap<>();
        runtimes.put("total", runtimeList.size());
        runtimes.put("online", rtOnline.get());
        runtimes.put("offline", runtimeList.size() - rtOnline.get());
        runtimes.put("list", runtimeList);
        result.put("runtimes", runtimes);

        return result;
    }

    /**
     * 健康中心总览（KPI + 告警）。任务/设备全量列表走分页接口。
     */
    @GetMapping("/health-center")
    public Map<String, Object> healthCenter() {
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("taskKpi", loadTaskKpi());
        data.put("deviceKpi", loadDeviceKpi());
        data.put("infraKpi", loadInfraKpi());
        data.put("infraDetails", loadInfraDetails());
        data.put("alerts", loadAlerts());
        return data;
    }

    /** 全量任务列表（分页） */
    @GetMapping("/health-center/tasks")
    public Map<String, Object> taskList(@RequestParam(defaultValue = "0") int page,
                                        @RequestParam(defaultValue = "20") int size,
                                        @RequestParam(required = false) String status,
                                        @RequestParam(required = false) String agentType,
                                        @RequestParam(required = false) String repo) {
        Map<String, Object> result = new LinkedHashMap<>();
        List<Object> params = new ArrayList<>();
        StringBuilder where = new StringBuilder(" WHERE 1=1");

        if (status != null && !status.isBlank()) {
            where.append(" AND status=?");
            params.add(status);
        }
        if (agentType != null && !agentType.isBlank()) {
            where.append(" AND agent_type=?");
            params.add(agentType);
        }
        if (repo != null && !repo.isBlank()) {
            where.append(" AND repo_key LIKE ?");
            params.add("%" + repo + "%");
        }

        // 排序：running 优先 → failed → 最近创建
        String orderBy = " ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'dispatched' THEN 2 WHEN 'failed' THEN 3 WHEN 'completed' THEN 4 ELSE 5 END, created_at DESC";

        long total = 0;
        try {
            total = grapeJdbc.queryForObject("SELECT COUNT(*) FROM agent_tasks" + where, Long.class, params.toArray());
        } catch (Exception e) { /* ignore */ }

        int offset = page * size;
        List<Map<String, Object>> list = new ArrayList<>();
        try {
            String sql = "SELECT id, repo_key, agent_type, status, issue_id, created_at, completed_at, " +
                    "result_text, errors, exit_code, event_seq, skill, runtime_id" +
                    " FROM agent_tasks" + where + orderBy + " LIMIT ? OFFSET ?";
            List<Object> queryParams = new ArrayList<>(params);
            queryParams.add(size);
            queryParams.add(offset);
            grapeJdbc.query(sql, rs -> {
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("id", rs.getString("id"));
                row.put("repoKey", rs.getString("repo_key"));
                row.put("agentType", rs.getString("agent_type"));
                row.put("status", rs.getString("status"));
                row.put("issueId", rs.getString("issue_id"));
                row.put("createdAt", rs.getString("created_at"));
                row.put("completedAt", rs.getString("completed_at"));
                row.put("duration", formatDuration(rs.getString("created_at"), rs.getString("completed_at")));
                row.put("skill", rs.getString("skill"));
                row.put("eventSeq", rs.getObject("event_seq"));
                row.put("exitCode", rs.getObject("exit_code"));
                String err = rs.getString("errors");
                String resultText = rs.getString("result_text");
                row.put("errorText", err != null && !err.isEmpty() ? truncate(err, 150) : "");
                row.put("resultText", resultText != null ? truncate(resultText, 150) : "");
                list.add(row);
            }, queryParams.toArray());
        } catch (Exception e) { /* ignore */ }

        result.put("content", list);
        result.put("totalElements", total);
        result.put("totalPages", (int) Math.ceil((double) total / size));
        result.put("page", page);
        result.put("size", size);
        return result;
    }

    /** 全量设备列表（分页） */
    @GetMapping("/health-center/devices")
    public Map<String, Object> deviceList(@RequestParam(defaultValue = "0") int page,
                                          @RequestParam(defaultValue = "20") int size,
                                          @RequestParam(required = false) String status,
                                          @RequestParam(required = false) String health,
                                          @RequestParam(required = false) String name) {
        Map<String, Object> result = new LinkedHashMap<>();
        List<Object> params = new ArrayList<>();
        StringBuilder where = new StringBuilder(" WHERE 1=1");

        if (status != null && !status.isBlank()) {
            where.append(" AND status=?");
            params.add(status);
        }
        if (name != null && !name.isBlank()) {
            where.append(" AND name LIKE ?");
            params.add("%" + name + "%");
        }

        long total = 0;
        try {
            total = grapeJdbc.queryForObject("SELECT COUNT(*) FROM agent_runtimes" + where, Long.class, params.toArray());
        } catch (Exception e) { /* ignore */ }

        int offset = page * size;
        List<Map<String, Object>> list = new ArrayList<>();
        try {
            String sql = "SELECT id, name, scope, status, healthy, busy_slots, max_concurrent, " +
                    "version, last_seen_at, cli_path, credential_id" +
                    " FROM agent_runtimes" + where +
                    " ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END, last_seen_at DESC" +
                    " LIMIT ? OFFSET ?";
            List<Object> queryParams = new ArrayList<>(params);
            queryParams.add(size);
            queryParams.add(offset);
            grapeJdbc.query(sql, rs -> {
                Map<String, Object> row = new LinkedHashMap<>();
                row.put("id", rs.getInt("id"));
                row.put("name", rs.getString("name"));
                row.put("scope", rs.getString("scope"));
                row.put("status", rs.getString("status"));
                String stat = rs.getString("status");
                String lastSeen = rs.getString("last_seen_at");
                String h = healthService.engineHealth(stat, lastSeen);
                row.put("health", h);
                row.put("busySlots", rs.getInt("busy_slots"));
                row.put("maxConcurrent", rs.getInt("max_concurrent"));
                row.put("version", rs.getString("version"));
                row.put("lastSeenAt", lastSeen);
                row.put("credentialId", rs.getObject("credential_id"));
                list.add(row);
            }, queryParams.toArray());
        } catch (Exception e) { /* ignore */ }

        // health 过滤在 Java 层做（health 是派生值不在 DB 中）
        if (health != null && !health.isBlank()) {
            list.removeIf(r -> !health.equals(r.get("health")));
            total = list.size();
        }

        result.put("content", list);
        result.put("totalElements", total);
        result.put("totalPages", (int) Math.ceil((double) total / size));
        result.put("page", page);
        result.put("size", size);
        return result;
    }

    // ===== 任务 KPI =====
    private Map<String, Object> loadTaskKpi() {
        Map<String, Object> kpi = new LinkedHashMap<>();
        try {
            kpi.put("total", count("SELECT COUNT(*) FROM agent_tasks"));
            kpi.put("completed", count("SELECT COUNT(*) FROM agent_tasks WHERE status='completed'"));
            kpi.put("running", count("SELECT COUNT(*) FROM agent_tasks WHERE status IN ('running','dispatched','queued')"));
            kpi.put("failed", count("SELECT COUNT(*) FROM agent_tasks WHERE status='failed'"));
            kpi.put("queued", count("SELECT COUNT(*) FROM agent_tasks WHERE status='queued'"));
            // stuck: running 超 15 分钟
            kpi.put("stuck", count("SELECT COUNT(*) FROM agent_tasks WHERE status='running' AND created_at < DATE_SUB(NOW(), INTERVAL 15 MINUTE)"));
        } catch (Exception e) {
            kpi.put("total", 0); kpi.put("completed", 0); kpi.put("running", 0); kpi.put("failed", 0); kpi.put("queued", 0); kpi.put("stuck", 0);
        }
        return kpi;
    }

    // ===== 设备 KPI =====
    private Map<String, Object> loadDeviceKpi() {
        Map<String, Object> kpi = new LinkedHashMap<>();
        try {
            long total = count("SELECT COUNT(*) FROM agent_runtimes");
            long online = count("SELECT COUNT(*) FROM agent_runtimes WHERE status='online'");
            long offline = count("SELECT COUNT(*) FROM agent_runtimes WHERE status='offline'");
            kpi.put("total", total);
            kpi.put("online", online);
            kpi.put("offline", offline);
            kpi.put("unhealthy", total - online);
        } catch (Exception e) {
            kpi.put("total", 0); kpi.put("online", 0); kpi.put("offline", 0); kpi.put("unhealthy", 0);
        }
        return kpi;
    }

    // ===== 基础设施 KPI =====
    private Map<String, Object> loadInfraKpi() {
        Map<String, Object> kpi = new LinkedHashMap<>();
        boolean dbHealthy = true;
        String dbError = null;
        try {
            grapeJdbc.queryForObject("SELECT 1", Integer.class);
        } catch (Exception e) {
            dbHealthy = false;
            dbError = truncate(e.getMessage(), 100);
        }
        kpi.put("dbCount", 1);
        kpi.put("dbHealthy", dbHealthy);
        kpi.put("elbCount", 0);
        kpi.put("dbError", dbError);
        return kpi;
    }

    // ===== 基础设施详情 =====
    private List<Map<String, Object>> loadInfraDetails() {
        List<Map<String, Object>> list = new ArrayList<>();
        Map<String, Object> db = new LinkedHashMap<>();
        db.put("name", "grape_test_db");
        db.put("type", "mysql");
        db.put("host", "127.0.0.1");
        db.put("port", 3306);

        long start = System.currentTimeMillis();
        boolean healthy = true;
        String error = null;
        try {
            grapeJdbc.queryForObject("SELECT 1", Integer.class);
        } catch (Exception e) {
            healthy = false;
            error = truncate(e.getMessage(), 100);
        }
        long latency = System.currentTimeMillis() - start;

        db.put("healthy", healthy);
        db.put("latency", latency);
        db.put("error", error);

        try {
            Integer connCount = grapeJdbc.queryForObject("SHOW STATUS LIKE 'Threads_connected'", (rs, i) -> rs.getInt(2));
            db.put("connections", connCount);
        } catch (Exception e) {
            db.put("connections", null);
        }

        list.add(db);
        return list;
    }

    // ===== 告警汇总（只收集异常项） =====
    private List<Map<String, Object>> loadAlerts() {
        List<Map<String, Object>> alerts = new ArrayList<>();

        // 1. 失败任务 → critical
        try {
            grapeJdbc.query("SELECT id, repo_key, agent_type, issue_id, created_at, errors FROM agent_tasks WHERE status='failed' ORDER BY created_at DESC LIMIT 30",
                    rs -> {
                        Map<String, Object> a = new LinkedHashMap<>();
                        a.put("dimension", "tasks");
                        a.put("severity", "critical");
                        a.put("title", "任务失败");
                        String err = rs.getString("errors");
                        a.put("detail", rs.getString("agent_type") + " · " + (err != null && !err.isEmpty() ? truncate(err, 80) : "未知错误"));
                        a.put("source", rs.getString("repo_key"));
                        a.put("time", rs.getString("created_at"));
                        alerts.add(a);
                    });
        } catch (Exception e) { /* ignore */ }

        // 2. 卡住的任务（running 超 15 分钟）→ warning
        try {
            grapeJdbc.query("SELECT id, repo_key, agent_type, created_at FROM agent_tasks WHERE status='running' AND created_at < DATE_SUB(NOW(), INTERVAL 15 MINUTE) LIMIT 30",
                    rs -> {
                        Map<String, Object> a = new LinkedHashMap<>();
                        a.put("dimension", "tasks");
                        a.put("severity", "warning");
                        a.put("title", "任务卡住");
                        a.put("detail", rs.getString("agent_type") + " 运行超过15分钟");
                        a.put("source", rs.getString("repo_key"));
                        a.put("time", rs.getString("created_at"));
                        alerts.add(a);
                    });
        } catch (Exception e) { /* ignore */ }

        // 3. 排队任务 → info
        try {
            grapeJdbc.query("SELECT id, repo_key, agent_type, created_at FROM agent_tasks WHERE status='queued' ORDER BY created_at DESC LIMIT 10",
                    rs -> {
                        Map<String, Object> a = new LinkedHashMap<>();
                        a.put("dimension", "tasks");
                        a.put("severity", "info");
                        a.put("title", "任务排队");
                        a.put("detail", rs.getString("agent_type") + " 等待引擎 claim");
                        a.put("source", rs.getString("repo_key"));
                        a.put("time", rs.getString("created_at"));
                        alerts.add(a);
                    });
        } catch (Exception e) { /* ignore */ }

        // 4. 离线设备 → warning
        try {
            grapeJdbc.query("SELECT id, name, status, last_seen_at FROM agent_runtimes WHERE status != 'online'",
                    rs -> {
                        Map<String, Object> a = new LinkedHashMap<>();
                        a.put("dimension", "devices");
                        a.put("severity", "warning");
                        a.put("title", "设备离线");
                        a.put("detail", rs.getString("name") + " 状态: " + rs.getString("status"));
                        a.put("source", "engine#" + rs.getInt("id"));
                        a.put("time", rs.getString("last_seen_at"));
                        alerts.add(a);
                    });
        } catch (Exception e) { /* ignore */ }

        // 5. 数据库不可达 → critical
        try {
            grapeJdbc.queryForObject("SELECT 1", Integer.class);
        } catch (Exception e) {
            Map<String, Object> a = new LinkedHashMap<>();
            a.put("dimension", "infra");
            a.put("severity", "critical");
            a.put("title", "数据库不可达");
            a.put("detail", "grape_test_db 连接失败");
            a.put("source", "127.0.0.1:3306");
            a.put("time", new Date().toString());
            alerts.add(a);
        }

        return alerts;
    }

    // ===== 工具方法 =====
    private long count(String sql, Object... params) {
        try {
            Long val = grapeJdbc.queryForObject(sql, Long.class, params);
            return val != null ? val : 0;
        } catch (Exception e) {
            return 0;
        }
    }

    private String truncate(String s, int max) {
        if (s == null) return "";
        return s.length() > max ? s.substring(0, max) + "..." : s;
    }

    // ===== 任务日志 =====
    /** 任务日志消息列表（直读 task_messages 表） */
    @GetMapping("/health-center/tasks/{taskId}/messages")
    public List<Map<String, Object>> taskMessages(@PathVariable String taskId) {
        List<Map<String, Object>> list = new ArrayList<>();
        try {
            grapeJdbc.query("SELECT seq, type, tool, content, input_json, output, created_at "
                    + "FROM task_messages WHERE task_id=? ORDER BY seq",
                    rs -> {
                        Map<String, Object> m = new LinkedHashMap<>();
                        m.put("seq", rs.getInt("seq"));
                        m.put("type", rs.getString("type"));
                        m.put("tool", rs.getString("tool"));
                        m.put("content", rs.getString("content"));
                        m.put("inputJson", rs.getString("input_json"));
                        m.put("output", rs.getString("output"));
                        m.put("createdAt", rs.getString("created_at"));
                        list.add(m);
                    }, taskId);
        } catch (Exception e) { /* ignore */ }
        return list;
    }

    /** 任务详情（含时间线信息） */
    @GetMapping("/health-center/tasks/{taskId}/info")
    public Map<String, Object> taskInfo(@PathVariable String taskId) {
        Map<String, Object> info = new LinkedHashMap<>();
        try {
            grapeJdbc.query("SELECT id, repo_key, agent_type, status, issue_id, created_at, completed_at, "
                    + "started_at, exit_code, result_text, errors, event_seq, skill, runtime_id "
                    + "FROM agent_tasks WHERE id=?", rs -> {
                info.put("id", rs.getString("id"));
                info.put("repoKey", rs.getString("repo_key"));
                info.put("agentType", rs.getString("agent_type"));
                info.put("status", rs.getString("status"));
                info.put("issueId", rs.getString("issue_id"));
                info.put("createdAt", rs.getString("created_at"));
                info.put("completedAt", rs.getString("completed_at"));
                info.put("startedAt", rs.getString("started_at"));
                info.put("exitCode", rs.getObject("exit_code"));
                info.put("resultText", rs.getString("result_text"));
                info.put("errors", rs.getString("errors"));
                info.put("eventSeq", rs.getObject("event_seq"));
                info.put("skill", rs.getString("skill"));
            }, taskId);
        } catch (Exception e) { /* ignore */ }
        return info;
    }

    /** 引擎任务列表（正在运行 + 历史，对齐主服务 runtime sessions） */
    @GetMapping("/health-center/runtimes/{runtimeId}/tasks")
    public Map<String, Object> runtimeTasks(@PathVariable int runtimeId,
                                            @RequestParam(required = false) String status,
                                            @RequestParam(required = false, defaultValue = "50") int limit) {
        Map<String, Object> result = new LinkedHashMap<>();
        List<Map<String, Object>> tasks = new ArrayList<>();

        StringBuilder sql = new StringBuilder("SELECT id, repo_key, agent_type, skill, status, exit_code, "
                + "result_text, created_at, started_at, completed_at, issue_id, event_seq "
                + "FROM agent_tasks WHERE runtime_id=?");
        List<Object> params = new ArrayList<>();
        params.add(runtimeId);
        if (status != null && !status.isBlank()) {
            sql.append(" AND status=?");
            params.add(status);
        }
        sql.append(" ORDER BY created_at DESC LIMIT ?");
        params.add(limit);

        try {
            grapeJdbc.query(sql.toString(), rs -> {
                Map<String, Object> t = new LinkedHashMap<>();
                t.put("id", rs.getString("id"));
                t.put("repoKey", rs.getString("repo_key"));
                t.put("agentType", rs.getString("agent_type"));
                t.put("skill", rs.getString("skill"));
                t.put("status", rs.getString("status"));
                t.put("exitCode", rs.getObject("exit_code"));
                t.put("resultText", truncate(rs.getString("result_text"), 200));
                t.put("createdAt", rs.getString("created_at"));
                t.put("startedAt", rs.getString("started_at"));
                t.put("completedAt", rs.getString("completed_at"));
                t.put("issueId", rs.getString("issue_id"));
                t.put("eventSeq", rs.getObject("event_seq"));
                tasks.add(t);
            }, params.toArray());
        } catch (Exception e) { /* ignore */ }

        // 分为正在运行和已完成
        List<Map<String, Object>> running = new ArrayList<>();
        List<Map<String, Object>> history = new ArrayList<>();
        for (Map<String, Object> t : tasks) {
            String s = (String) t.get("status");
            if ("running".equals(s) || "queued".equals(s) || "dispatched".equals(s)) {
                running.add(t);
            } else {
                history.add(t);
            }
        }

        result.put("runtimeId", runtimeId);
        result.put("running", running);
        result.put("history", history);
        result.put("total", tasks.size());
        return result;
    }

    private String formatDuration(String created, String completed) {
        if (created == null || completed == null || completed.isEmpty()) return "—";
        try {
            long start = java.sql.Timestamp.valueOf(created.replace("T", " ").replace("Z", "")).getTime();
            long end = completed.isEmpty() ? System.currentTimeMillis() :
                    java.sql.Timestamp.valueOf(completed.replace("T", " ").replace("Z", "")).getTime();
            long diff = (end - start) / 1000;
            if (diff < 0) return "—";
            if (diff < 60) return diff + "s";
            if (diff < 3600) return (diff / 60) + "m" + (diff % 60) + "s";
            return (diff / 3600) + "h" + ((diff % 3600) / 60) + "m";
        } catch (Exception e) {
            return "—";
        }
    }

    // ===== 任务时序（按 Issue 分组的事件链，对齐主服务 ops/timeline） =====
    @GetMapping("/health-center/timeline")
    public Map<String, Object> timeline(@RequestParam(required = false) String repo) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("repoKey", repo != null ? repo : "");

        StringBuilder where = new StringBuilder(" WHERE issue_id IS NOT NULL AND issue_id <> ''");
        List<Object> params = new ArrayList<>();
        if (repo != null && !repo.isBlank()) {
            where.append(" AND repo_key LIKE ?");
            params.add("%" + repo + "%");
        }

        List<Map<String, Object>> rows;
        try {
            rows = grapeJdbc.queryForList(
                    "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload, repo_key"
                            + " FROM events" + where + " ORDER BY created_at",
                    params.toArray());
        } catch (Exception e) {
            rows = List.of();
        }

        // 查 agent_tasks 建立 (issue_id, event_seq) → task_id 映射
        Map<String, Map<Integer, String>> taskMap = new HashMap<>();
        try {
            grapeJdbc.query("SELECT issue_id, event_seq, id FROM agent_tasks WHERE issue_id IS NOT NULL AND issue_id <> ''",
                    rs -> {
                        String iid = rs.getString("issue_id");
                        int seq = rs.getInt("event_seq");
                        String tid = rs.getString("id");
                        taskMap.computeIfAbsent(iid, k -> new HashMap<>()).put(seq, tid);
                    });
        } catch (Exception e) { /* ignore */ }

        // 按 issue_id 分组
        Map<String, List<Map<String, Object>>> group = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            String issueId = (String) row.get("issue_id");
            group.computeIfAbsent(issueId, k -> new ArrayList<>()).add(row);
        }

        ObjectMapper om = new ObjectMapper();
        List<Map<String, Object>> issues = new ArrayList<>();
        for (Map.Entry<String, List<Map<String, Object>>> e : group.entrySet()) {
            Map<String, Object> g = new LinkedHashMap<>();
            g.put("issueId", e.getKey());
            List<Map<String, Object>> evs = e.getValue();
            // 从 repo_key 提取仓库名（gitcode:owner/repo → owner/repo）
            String repoKey = (String) evs.get(0).get("repo_key");
            String repoName = repoKey != null ? repoKey.replaceFirst("^gitcode:", "") : "";
            g.put("repoName", repoName);

            // 从 payload 提取 issue 标题
            String issueTitle = "";
            for (Map<String, Object> ev : evs) {
                if (issueTitle.isEmpty()) {
                    issueTitle = extractIssueTitle(ev.get("payload"), om);
                }
            }
            g.put("title", issueTitle);
            g.put("eventCount", evs.size());
            g.put("firstAt", evs.get(0).get("created_at"));
            g.put("lastAt", evs.get(evs.size() - 1).get("created_at"));

            // 精简事件字段 + 附带 taskId
            List<Map<String, Object>> slim = new ArrayList<>();
            Map<Integer, String> issueTasks = taskMap.getOrDefault(e.getKey(), Map.of());
            for (Map<String, Object> ev : evs) {
                Map<String, Object> s = new LinkedHashMap<>();
                int seq = ev.get("seq") != null ? ((Number) ev.get("seq")).intValue() : 0;
                s.put("seq", seq);
                s.put("eventType", ev.get("event_type"));
                s.put("agentType", ev.get("agent_type"));
                s.put("skill", ev.get("skill"));
                s.put("status", ev.get("status"));
                s.put("duration", ev.get("duration"));
                s.put("createdAt", ev.get("created_at"));
                s.put("actor", ev.get("actor"));
                s.put("taskId", issueTasks.getOrDefault(seq, ""));
                slim.add(s);
            }
            g.put("events", slim);
            issues.add(g);
        }
        result.put("issues", issues);
        return result;
    }

    /** 从 payload JSON 提取 issue 标题（GitCode webhook 格式：object_attributes.title） */
    @SuppressWarnings("unchecked")
    private String extractIssueTitle(Object payload, ObjectMapper om) {
        if (payload == null) return "";
        try {
            String json = payload.toString();
            Map<String, Object> data = om.readValue(json, Map.class);
            Map<String, Object> attrs = (Map<String, Object>) data.get("object_attributes");
            if (attrs != null) {
                Object title = attrs.get("title");
                if (title != null) return title.toString();
            }
        } catch (Exception e) { /* ignore */ }
        return "";
    }
}
