package com.grape.admin.service;

import com.grape.admin.common.NotFoundException;
import com.grape.admin.entity.readonly.AgentTaskRo;
import com.grape.admin.entity.readonly.AgentTypeRo;
import com.grape.admin.repository.readonly.AgentTaskRoRepository;
import com.grape.admin.repository.readonly.AgentTypeRoRepository;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 智能体管理：读 agent_type（grape 共享库）+ agent_tasks 聚合；写 enable 开关。
 */
@Service
public class AgentService {

    private final AgentTypeRoRepository agentTypeRepository;
    private final AgentTaskRoRepository taskRepository;
    private final JdbcTemplate jdbc;
    private final AdminAuditService auditService;

    public AgentService(AgentTypeRoRepository agentTypeRepository,
                        AgentTaskRoRepository taskRepository,
                        JdbcTemplate jdbc,
                        AdminAuditService auditService) {
        this.agentTypeRepository = agentTypeRepository;
        this.taskRepository = taskRepository;
        this.jdbc = jdbc;
        this.auditService = auditService;
    }

    /**
     * 智能体列表 + 任务聚合：
     * {
     *   agents: [{agentType, displayName, description, icon, orderNum, enable,
     *             defaultRuntimeId, subscribeEvents, taskCount, statusBreakdown:{...}}],
     *   taskCountByAgent: {vod: 7, ...},
     *   taskStatusBreakdown: {vod: {completed: 5, running: 2}, ...},
     *   total: 5, enabledCount: 4
     * }
     */
    public Map<String, Object> list() {
        List<AgentTypeRo> types = agentTypeRepository.findAllByOrderByOrderNumAsc();

        // 每个 agent 的任务总数
        Map<String, Long> countByType = new LinkedHashMap<>();
        for (Object[] row : taskRepository.countByAgentType()) {
            countByType.put(String.valueOf(row[0]), ((Number) row[1]).longValue());
        }
        // 每个 agent × 状态
        Map<String, Map<String, Long>> statusByType = new LinkedHashMap<>();
        for (Object[] row : taskRepository.countByAgentTypeAndStatus()) {
            String type = String.valueOf(row[0]);
            String status = String.valueOf(row[1]);
            long n = ((Number) row[2]).longValue();
            statusByType.computeIfAbsent(type, k -> new LinkedHashMap<>()).put(status, n);
        }

        List<Map<String, Object>> agents = new ArrayList<>();
        for (AgentTypeRo t : types) {
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
            agents.add(m);
        }

        long enabled = agents.stream().filter(a -> Integer.valueOf(1).equals(a.get("enable"))).count();

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("agents", agents);
        result.put("taskCountByAgent", countByType);
        result.put("taskStatusBreakdown", statusByType);
        result.put("total", agents.size());
        result.put("enabledCount", enabled);
        return result;
    }

    /** 切换智能体启用状态，返回切换后的 enable（1/0）。 */
    public Integer toggleEnable(String agentType, boolean enable, String operator) {
        int n = jdbc.update("UPDATE agent_type SET enable = ? WHERE agent_type = ?", enable ? 1 : 0, agentType);
        if (n == 0) {
            throw new NotFoundException("智能体不存在: " + agentType);
        }
        auditService.record(null, operator, "agent.enable", "agent:" + agentType, "enable=" + (enable ? 1 : 0));
        return enable ? 1 : 0;
    }
}
