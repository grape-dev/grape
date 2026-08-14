package com.grape.admin.service;

import com.grape.admin.common.BizException;
import com.grape.admin.common.NotFoundException;
import com.grape.admin.entity.readonly.AgentTaskRo;
import com.grape.admin.entity.readonly.AgentTypeRo;
import com.grape.admin.repository.readonly.AgentTaskRoRepository;
import com.grape.admin.repository.readonly.AgentTypeRoRepository;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowCallbackHandler;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 智能体管理：读 agent_type（grape 共享库）+ agent_tasks / devops_agent_session / issue_agent_session 聚合；写 enable 开关与负载预警阈值。
 *
 * 分类口径（运营台约定）：
 * - 官方 official = agent_type 表平台内置智能体（vod/issue/coding/qa/sre）
 * - 仓库 repo     = devops_agent_session 按 (repo_key, agent_type) 的仓库级实例
 * - 个人 personal = 暂无数据源，预留分类
 *
 * 负载率口径：进行中任务数(queued/running/dispatched) ÷ (在线引擎实例数 × 单实例默认并发 20)
 */
@Service
public class AgentService {

    /** 与 worker RUNTIME_MAX_CONCURRENT_TASKS 默认值对齐 */
    private static final int DEFAULT_MAX_CONCURRENT = 20;
    private static final int DEFAULT_ALERT_THRESHOLD_PCT = 80;
    private static final List<String> IN_FLIGHT_STATUSES = List.of("queued", "running", "dispatched");

    private final AgentTypeRoRepository agentTypeRepository;
    private final AgentTaskRoRepository taskRepository;
    private final JdbcTemplate grapeJdbc;
    private final JdbcTemplate adminJdbc;
    private final AdminAuditService auditService;

    public AgentService(AgentTypeRoRepository agentTypeRepository,
                        AgentTaskRoRepository taskRepository,
                        @Qualifier("grapeJdbcTemplate") JdbcTemplate grapeJdbc,
                        @Qualifier("adminJdbcTemplate") JdbcTemplate adminJdbc,
                        AdminAuditService auditService) {
        this.agentTypeRepository = agentTypeRepository;
        this.taskRepository = taskRepository;
        this.grapeJdbc = grapeJdbc;
        this.adminJdbc = adminJdbc;
        this.auditService = auditService;
    }

    /**
     * 智能体列表 + 任务聚合：
     * {
     *   agents: [{agentType, displayName, description, icon, orderNum, enable,
     *             defaultRuntimeId, subscribeEvents, category, taskCount, statusBreakdown,
     *             sessionCount, repoUsageCount, inFlightTasks, loadRate, loadLevel, alertThresholdPct}],
     *   repoAgents: [{id, repoKey, agentType, status, enable, runtimeId, lastActiveAt}],
     *   onlineRuntimes: N, maxConcurrentPerRuntime: 20,
     *   total, enabledCount
     * }
     */
    public Map<String, Object> list() {
        List<AgentTypeRo> types = agentTypeRepository.findAllByOrderByOrderNumAsc();
        long onlineRuntimes = count("SELECT COUNT(*) FROM agent_runtimes WHERE status='online'");
        Map<String, Integer> alertMap = loadAlertThresholds();

        // 每个 agent 的任务总数与状态分布
        Map<String, Long> countByType = new LinkedHashMap<>();
        for (Object[] row : taskRepository.countByAgentType()) {
            countByType.put(String.valueOf(row[0]), ((Number) row[1]).longValue());
        }
        Map<String, Map<String, Long>> statusByType = new LinkedHashMap<>();
        for (Object[] row : taskRepository.countByAgentTypeAndStatus()) {
            String type = String.valueOf(row[0]);
            String status = String.valueOf(row[1]);
            long n = ((Number) row[2]).longValue();
            statusByType.computeIfAbsent(type, k -> new LinkedHashMap<>()).put(status, n);
        }
        // 进行中任务数（queued/running/dispatched）
        Map<String, Long> inFlightByType = inFlightTaskCountByAgentType();
        // 会话数与仓库使用数（devops_agent_session 为仓库级实例主表；issue 会话并入会话数）
        Map<String, Long> sessionByType = countByColumn("devops_agent_session", "agent_type");
        Map<String, Long> issueSessionByType = countByColumn("issue_agent_session", "agent_type");
        Map<String, Long> repoUsageByType = distinctRepoCountByAgentType();

        List<Map<String, Object>> agents = new ArrayList<>();
        for (AgentTypeRo t : types) {
            String type = t.getAgentType();
            long sessions = sessionByType.getOrDefault(type, 0L) + issueSessionByType.getOrDefault(type, 0L);
            long inFlight = inFlightByType.getOrDefault(type, 0L);
            long repoUsage = repoUsageByType.getOrDefault(type, 0L);
            Map<String, Object> m = baseAgentMap(t, countByType, statusByType);
            m.put("category", "official");
            m.put("sessionCount", sessions);
            m.put("repoUsageCount", repoUsage);
            m.put("inFlightTasks", inFlight);
            putLoad(m, inFlight, onlineRuntimes, alertMap.getOrDefault(type, DEFAULT_ALERT_THRESHOLD_PCT));
            agents.add(m);
        }

        long enabled = agents.stream().filter(a -> Integer.valueOf(1).equals(a.get("enable"))).count();

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("agents", agents);
        result.put("repoAgents", listRepoAgents());
        result.put("onlineRuntimes", onlineRuntimes);
        result.put("maxConcurrentPerRuntime", DEFAULT_MAX_CONCURRENT);
        result.put("taskCountByAgent", countByType);
        result.put("taskStatusBreakdown", statusByType);
        result.put("total", agents.size());
        result.put("enabledCount", enabled);
        return result;
    }

    /** 智能体详情（卡片点击进入）：基本信息 + 指标 + 仓库使用明细 + 任务状态分布 + 预警值 */
    public Map<String, Object> detail(String agentType) {
        AgentTypeRo t = agentTypeRepository.findById(agentType)
                .orElseThrow(() -> new NotFoundException("智能体不存在: " + agentType));
        long onlineRuntimes = count("SELECT COUNT(*) FROM agent_runtimes WHERE status='online'");
        Map<String, Integer> alertMap = loadAlertThresholds();

        Map<String, Long> countByType = new LinkedHashMap<>();
        for (Object[] row : taskRepository.countByAgentType()) {
            countByType.put(String.valueOf(row[0]), ((Number) row[1]).longValue());
        }
        Map<String, Map<String, Long>> statusByType = new LinkedHashMap<>();
        for (Object[] row : taskRepository.countByAgentTypeAndStatus()) {
            String type = String.valueOf(row[0]);
            String status = String.valueOf(row[1]);
            long n = ((Number) row[2]).longValue();
            statusByType.computeIfAbsent(type, k -> new LinkedHashMap<>()).put(status, n);
        }
        long inFlight = inFlightTaskCountByAgentType().getOrDefault(agentType, 0L);
        long sessions = countByColumn("devops_agent_session", "agent_type").getOrDefault(agentType, 0L)
                + countByColumn("issue_agent_session", "agent_type").getOrDefault(agentType, 0L);
        long repoUsage = distinctRepoCountByAgentType().getOrDefault(agentType, 0L);
        int threshold = alertMap.getOrDefault(agentType, DEFAULT_ALERT_THRESHOLD_PCT);

        Map<String, Object> m = baseAgentMap(t, countByType, statusByType);
        m.put("category", "official");
        m.put("sessionCount", sessions);
        m.put("repoUsageCount", repoUsage);
        m.put("inFlightTasks", inFlight);
        m.put("issueSessionCount", countByColumn("issue_agent_session", "agent_type").getOrDefault(agentType, 0L));
        putLoad(m, inFlight, onlineRuntimes, threshold);
        m.put("alertThresholdPct", threshold);
        m.put("onlineRuntimes", onlineRuntimes);
        m.put("maxConcurrentPerRuntime", DEFAULT_MAX_CONCURRENT);
        m.put("repoUsages", listRepoAgents(agentType));
        return m;
    }

    /** 设置负载预警阈值百分比（1-100），返回新阈值。 */
    public int setAlertThreshold(String agentType, int thresholdPct, String operator) {
        if (!agentTypeRepository.existsById(agentType)) {
            throw new NotFoundException("智能体不存在: " + agentType);
        }
        if (thresholdPct < 1 || thresholdPct > 100) {
            throw new BizException("预警阈值必须在 1-100 之间");
        }
        adminJdbc.update("INSERT INTO admin_agent_alert (agent_type, alert_threshold_pct, updated_by) VALUES (?, ?, ?) " +
                        "ON DUPLICATE KEY UPDATE alert_threshold_pct = VALUES(alert_threshold_pct), updated_by = VALUES(updated_by), updated_at = NOW()",
                agentType, thresholdPct, operator);
        auditService.record(null, operator, "agent.alert", "agent:" + agentType, "threshold_pct=" + thresholdPct);
        return thresholdPct;
    }

    /** 切换智能体启用状态，返回切换后的 enable（1/0）。 */
    public Integer toggleEnable(String agentType, boolean enable, String operator) {
        int n = grapeJdbc.update("UPDATE agent_type SET enable = ? WHERE agent_type = ?", enable ? 1 : 0, agentType);
        if (n == 0) {
            throw new NotFoundException("智能体不存在: " + agentType);
        }
        auditService.record(null, operator, "agent.enable", "agent:" + agentType, "enable=" + (enable ? 1 : 0));
        return enable ? 1 : 0;
    }

    // ===== 私有工具 =====

    private Map<String, Object> baseAgentMap(AgentTypeRo t, Map<String, Long> countByType,
                                             Map<String, Map<String, Long>> statusByType) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("agentType", t.getAgentType());
        m.put("displayName", t.getDisplayName());
        m.put("description", t.getDescription());
        m.put("icon", t.getIcon());
        m.put("orderNum", t.getOrderNum());
        m.put("enable", t.getEnable() != null ? t.getEnable() : 0);
        m.put("defaultRuntimeId", t.getDefaultRuntimeId());
        m.put("subscribeEvents", t.getSubscribeEvents());
        m.put("taskCount", countByType.getOrDefault(t.getAgentType(), 0L));
        m.put("statusBreakdown", statusByType.getOrDefault(t.getAgentType(), new LinkedHashMap<>()));
        return m;
    }

    private void putLoad(Map<String, Object> m, long inFlight, long onlineRuntimes, int threshold) {
        long capacity = onlineRuntimes * DEFAULT_MAX_CONCURRENT;
        double rate = capacity > 0 ? Math.round(inFlight * 1000.0 / capacity) / 10.0 : 0.0;
        m.put("loadRate", rate);
        m.put("loadLevel", rate >= threshold ? "over" : "ok");
        m.put("alertThresholdPct", threshold);
    }

    /** devops_agent_session 仓库级实例（全部 agent_type） */
    private List<Map<String, Object>> listRepoAgents() {
        return grapeJdbc.query(
                "SELECT id, repo_key, agent_type, status, enable, runtime_id, last_active_at " +
                        "FROM devops_agent_session ORDER BY agent_type, repo_key",
                (rs, rowNum) -> {
                    Map<String, Object> row = new LinkedHashMap<>();
                    row.put("id", rs.getString("id"));
                    row.put("repoKey", rs.getString("repo_key"));
                    row.put("agentType", rs.getString("agent_type"));
                    row.put("status", rs.getString("status"));
                    row.put("enable", rs.getInt("enable"));
                    row.put("runtimeId", rs.getInt("runtime_id"));
                    row.put("lastActiveAt", rs.getString("last_active_at"));
                    return row;
                });
    }

    /** 某 agent 的仓库级实例（详情页仓库使用明细） */
    private List<Map<String, Object>> listRepoAgents(String agentType) {
        return grapeJdbc.query(
                "SELECT id, repo_key, status, enable, runtime_id, last_active_at " +
                        "FROM devops_agent_session WHERE agent_type = ? ORDER BY repo_key",
                (rs, rowNum) -> {
                    Map<String, Object> row = new LinkedHashMap<>();
                    row.put("id", rs.getString("id"));
                    row.put("repoKey", rs.getString("repo_key"));
                    row.put("status", rs.getString("status"));
                    row.put("enable", rs.getInt("enable"));
                    row.put("runtimeId", rs.getInt("runtime_id"));
                    row.put("lastActiveAt", rs.getString("last_active_at"));
                    return row;
                }, agentType);
    }

    private Map<String, Integer> loadAlertThresholds() {
        Map<String, Integer> m = new LinkedHashMap<>();
        try {
            adminJdbc.query("SELECT agent_type, alert_threshold_pct FROM admin_agent_alert",
                    (RowCallbackHandler) rs -> m.put(rs.getString(1), rs.getInt(2)));
        } catch (Exception e) {
            // 表未迁移（老部署）时静默降级为默认阈值，不让智能体页挂掉
        }
        return m;
    }

    private Map<String, Long> inFlightTaskCountByAgentType() {
        Map<String, Long> m = new LinkedHashMap<>();
        String in = IN_FLIGHT_STATUSES.stream()
                .map(s -> "'" + s + "'")
                .collect(java.util.stream.Collectors.joining(","));
        grapeJdbc.query("SELECT COALESCE(agent_type,''), COUNT(*) FROM agent_tasks WHERE status IN (" + in + ") GROUP BY agent_type",
                (RowCallbackHandler) rs -> m.put(rs.getString(1), rs.getLong(2)));
        return m;
    }

    private Map<String, Long> distinctRepoCountByAgentType() {
        Map<String, Long> m = new LinkedHashMap<>();
        grapeJdbc.query("SELECT agent_type, COUNT(DISTINCT repo_key) FROM devops_agent_session GROUP BY agent_type",
                (RowCallbackHandler) rs -> m.put(rs.getString(1), rs.getLong(2)));
        return m;
    }

    private Map<String, Long> countByColumn(String table, String column) {
        Map<String, Long> m = new LinkedHashMap<>();
        grapeJdbc.query("SELECT " + column + ", COUNT(*) FROM " + table + " GROUP BY " + column,
                (RowCallbackHandler) rs -> m.put(rs.getString(1), rs.getLong(2)));
        return m;
    }

    private long count(String sql) {
        Long v = grapeJdbc.queryForObject(sql, Long.class);
        return v != null ? v : 0L;
    }
}
