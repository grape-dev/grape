package com.grape.admin.service;

import com.grape.admin.entity.readonly.AgentRuntimeRo;
import com.grape.admin.entity.readonly.AgentTaskRo;
import com.grape.admin.entity.readonly.RuntimeCredentialRo;
import com.grape.admin.repository.readonly.AgentRuntimeRoRepository;
import com.grape.admin.repository.readonly.AgentTaskRoRepository;
import com.grape.admin.repository.readonly.RuntimeCredentialRoRepository;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * 运行时管理读服务：设备（runtime_credentials）+ 引擎（agent_runtimes）+ 任务（agent_tasks）聚合，直读共享库。
 */
@Service
public class RuntimeDeviceService {

    private final RuntimeCredentialRoRepository credentialRepository;
    private final AgentRuntimeRoRepository runtimeRepository;
    private final AgentTaskRoRepository taskRepository;
    private final HealthService healthService;

    public RuntimeDeviceService(RuntimeCredentialRoRepository credentialRepository,
                                AgentRuntimeRoRepository runtimeRepository,
                                AgentTaskRoRepository taskRepository,
                                HealthService healthService) {
        this.credentialRepository = credentialRepository;
        this.runtimeRepository = runtimeRepository;
        this.taskRepository = taskRepository;
        this.healthService = healthService;
    }

    public Map<String, Object> listCredentials(String scope, String owner, String status, String health, int page, int size) {
        List<RuntimeCredentialRo> credentials = credentialRepository.findAll();
        List<AgentRuntimeRo> runtimes = runtimeRepository.findAll();
        Map<Integer, Long> engineCountByCredential = runtimes.stream()
                .filter(r -> r.getCredentialId() != null)
                .collect(Collectors.groupingBy(AgentRuntimeRo::getCredentialId, Collectors.counting()));

        // 全量健康/状态分布统计（不随过滤变化，供页头徽章行）
        Map<String, Long> stats = new LinkedHashMap<>();
        for (String h : new String[]{"online", "recently_lost", "offline", "about_to_gc", "revoked"}) {
            stats.put(h, 0L);
        }
        for (RuntimeCredentialRo c : credentials) {
            String h = healthService.deviceHealth(c.getStatus(), c.getLastSeenAt());
            stats.merge(h, 1L, Long::sum);
        }

        List<Map<String, Object>> list = credentials.stream()
                .filter(c -> scope == null || scope.isBlank() || scope.equals(c.getScope()))
                .filter(c -> owner == null || owner.isBlank()
                        || (c.getOwnerName() != null && c.getOwnerName().toLowerCase().contains(owner.toLowerCase())))
                .filter(c -> status == null || status.isBlank() || status.equals(c.getStatus()))
                .filter(c -> health == null || health.isBlank()
                        || health.equals(healthService.deviceHealth(c.getStatus(), c.getLastSeenAt())))
                .map(c -> {
                    Map<String, Object> m = new LinkedHashMap<>();
                    m.put("id", c.getId());
                    m.put("name", c.getName());
                    m.put("deviceName", c.getDeviceName());
                    m.put("scope", c.getScope());
                    m.put("ownerId", c.getOwnerId());
                    m.put("ownerName", c.getOwnerName());
                    m.put("status", c.getStatus());
                    m.put("health", healthService.deviceHealth(c.getStatus(), c.getLastSeenAt()));
                    m.put("lastSeenAt", c.getLastSeenAt());
                    m.put("lastIp", c.getLastIp());
                    m.put("createdAt", c.getCreatedAt());
                    m.put("engineCount", engineCountByCredential.getOrDefault(c.getId(), 0L).intValue());
                    return m;
                })
                .collect(Collectors.toList());

        // 默认按最后心跳降序（最近活跃优先，null 排最后）
        list.sort((a, b) -> {
            String sa = (String) a.get("lastSeenAt");
            String sb = (String) b.get("lastSeenAt");
            if (sa == null && sb == null) return 0;
            if (sa == null) return 1;
            if (sb == null) return -1;
            return sb.compareTo(sa);
        });

        int total = list.size();
        int from = Math.min(page * size, total);
        int to = Math.min(from + size, total);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("content", list.subList(from, to));
        result.put("totalElements", total);
        result.put("totalPages", size > 0 ? (total + size - 1) / size : 0);
        result.put("page", page);
        result.put("size", size);
        result.put("stats", stats);
        return result;
    }

    public Map<String, Object> listDevices(String scope, String status, String health, String name, int page, int size) {
        List<RuntimeCredentialRo> credentials = credentialRepository.findAll();
        List<AgentRuntimeRo> runtimes = runtimeRepository.findAll();
        Map<Integer, List<AgentRuntimeRo>> enginesByCredential = runtimes.stream()
                .filter(r -> r.getCredentialId() != null)
                .collect(Collectors.groupingBy(AgentRuntimeRo::getCredentialId));
        Map<Integer, Long> taskCountByRuntime = taskRepository.findAll().stream()
                .filter(t -> t.getRuntimeId() != null)
                .collect(Collectors.groupingBy(AgentTaskRo::getRuntimeId, Collectors.counting()));

        // 全量健康分布统计（不随过滤/分页变化，供页头徽章行）
        Map<String, Long> stats = new LinkedHashMap<>();
        for (String h : new String[]{"online", "recently_lost", "offline", "about_to_gc", "revoked"}) {
            stats.put(h, 0L);
        }
        for (RuntimeCredentialRo c : credentials) {
            String h = healthService.deviceHealth(c.getStatus(), c.getLastSeenAt());
            stats.merge(h, 1L, Long::sum);
        }

        List<Map<String, Object>> filtered = new ArrayList<>();
        for (RuntimeCredentialRo c : credentials) {
            if (scope != null && !scope.isBlank() && !scope.equals(c.getScope())) continue;
            if (status != null && !status.isBlank() && !status.equals(c.getStatus())) continue;
            if (health != null && !health.isBlank()
                    && !health.equals(healthService.deviceHealth(c.getStatus(), c.getLastSeenAt()))) continue;
            if (name != null && !name.isBlank()
                    && !(c.getName() != null && c.getName().toLowerCase().contains(name.toLowerCase()))
                    && !(c.getDeviceName() != null && c.getDeviceName().toLowerCase().contains(name.toLowerCase()))) continue;
            List<AgentRuntimeRo> engines = enginesByCredential.getOrDefault(c.getId(), List.of());
            long taskCount = engines.stream()
                    .mapToLong(e -> taskCountByRuntime.getOrDefault(e.getId(), 0L)).sum();
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("id", c.getId());
            d.put("name", c.getName());
            d.put("deviceName", c.getDeviceName());
            d.put("status", c.getStatus());
            d.put("scope", c.getScope());
            d.put("ownerId", c.getOwnerId());
            d.put("ownerName", c.getOwnerName());
            d.put("health", healthService.deviceHealth(c.getStatus(), c.getLastSeenAt()));
            d.put("lastSeenAt", c.getLastSeenAt());
            d.put("lastIp", c.getLastIp());
            d.put("createdAt", c.getCreatedAt());
            d.put("engineCount", engines.size());
            d.put("taskCount", taskCount);
            filtered.add(d);
        }

        // 默认按最后心跳降序（最近活跃优先，null 排最后）
        filtered.sort((a, b) -> {
            String sa = (String) a.get("lastSeenAt");
            String sb = (String) b.get("lastSeenAt");
            if (sa == null && sb == null) return 0;
            if (sa == null) return 1;
            if (sb == null) return -1;
            return sb.compareTo(sa);
        });

        int total = filtered.size();
        int from = Math.min(page * size, total);
        int to = Math.min(from + size, total);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("content", filtered.subList(from, to));
        result.put("totalElements", total);
        result.put("totalPages", size > 0 ? (total + size - 1) / size : 0);
        result.put("page", page);
        result.put("size", size);
        result.put("stats", stats);
        return result;
    }

    public Map<String, Object> deviceDetail(Integer credentialId, int taskPage, int taskSize, Integer engineId,
                                            String taskStatus, String taskSkill, String taskType, String sortOrder) {
        RuntimeCredentialRo c = credentialRepository.findById(credentialId)
                .orElseThrow(() -> new NotFoundException("设备不存在: " + credentialId));
        List<AgentRuntimeRo> engines = runtimeRepository.findByCredentialId(credentialId);
        Map<String, Object> device = new LinkedHashMap<>();
        device.put("id", c.getId());
        device.put("name", c.getName());
        device.put("deviceName", c.getDeviceName());
        device.put("status", c.getStatus());
        device.put("health", healthService.deviceHealth(c.getStatus(), c.getLastSeenAt()));
        device.put("lastSeenAt", c.getLastSeenAt());
        device.put("lastIp", c.getLastIp());
        device.put("createdAt", c.getCreatedAt());

        List<Map<String, Object>> engineList = engines.stream().map(e -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", e.getId());
            m.put("name", e.getName());
            m.put("binary", e.getBinary());
            m.put("version", e.getVersion());
            m.put("healthy", e.getHealthy());
            m.put("status", e.getStatus());
            m.put("health", healthService.engineHealth(e.getStatus(), e.getLastSeenAt()));
            m.put("lastSeenAt", e.getLastSeenAt());
            m.put("cliPath", e.getCliPath());
            m.put("scope", e.getScope());
            return m;
        }).collect(Collectors.toList());

        Set<Integer> runtimeIds = engines.stream().map(AgentRuntimeRo::getId).collect(Collectors.toSet());
        List<AgentTaskRo> engineTasks = runtimeIds.isEmpty() ? List.of()
                : taskRepository.findAll().stream()
                        .filter(t -> t.getRuntimeId() != null && runtimeIds.contains(t.getRuntimeId()))
                        .filter(t -> engineId == null || engineId.equals(t.getRuntimeId()))   // 按引擎过滤关联任务
                        .collect(Collectors.toList());

        // 筛选选项统计（基于引擎过滤后的全量任务，不随筛选/分页变化，对齐 VOD 列头计数语义）
        Map<String, Object> taskFilterOptions = new LinkedHashMap<>();
        taskFilterOptions.put("status", countBy(engineTasks, AgentTaskRo::getStatus));
        taskFilterOptions.put("skill", countBy(engineTasks, AgentTaskRo::getSkill));
        taskFilterOptions.put("agentType", countBy(engineTasks, AgentTaskRo::getAgentType));

        List<AgentTaskRo> allTasks = engineTasks.stream()
                .filter(t -> taskStatus == null || taskStatus.isBlank() || taskStatus.equals(t.getStatus()))
                .filter(t -> taskType == null || taskType.isBlank() || taskType.equals(t.getAgentType()))
                .filter(t -> taskSkill == null || taskSkill.isBlank()
                        || (t.getSkill() != null && t.getSkill().toLowerCase().contains(taskSkill.toLowerCase())))
                .sorted((a, b) -> {
                    int cmp = String.valueOf(b.getCreatedAt()).compareTo(String.valueOf(a.getCreatedAt()));
                    return "asc".equalsIgnoreCase(sortOrder) ? -cmp : cmp;   // 默认 desc=最近在前
                })
                .collect(Collectors.toList());
        int total = allTasks.size();
        int from = Math.min(taskPage * taskSize, total);
        int to = Math.min(from + taskSize, total);
        Map<String, Object> taskPageObj = new LinkedHashMap<>();
        taskPageObj.put("content", allTasks.subList(from, to).stream().map(this::toTaskMap).collect(Collectors.toList()));
        taskPageObj.put("totalElements", total);
        taskPageObj.put("totalPages", taskSize > 0 ? (total + taskSize - 1) / taskSize : 0);
        taskPageObj.put("page", taskPage);
        taskPageObj.put("size", taskSize);
        taskPageObj.put("filterOptions", taskFilterOptions);

        device.put("engines", engineList);
        device.put("tasks", taskPageObj);
        return device;
    }

    /** 按字段分组计数（null 归入空串），供列头筛选选项展示 */
    private Map<String, Long> countBy(List<AgentTaskRo> tasks, java.util.function.Function<AgentTaskRo, String> f) {
        return tasks.stream()
                .map(t -> f.apply(t) == null ? "" : f.apply(t))
                .collect(Collectors.groupingBy(s -> s, LinkedHashMap::new, Collectors.counting()));
    }

    public Map<String, Object> engineList(String scope, String type, String device, String status, int page, int size) {
        List<AgentRuntimeRo> runtimes = runtimeRepository.findAll();
        Map<Integer, RuntimeCredentialRo> credentialById = credentialRepository.findAll().stream()
                .collect(Collectors.toMap(RuntimeCredentialRo::getId, c -> c));
        Map<Integer, String> deviceNameByCredential = credentialById.entrySet().stream()
                .collect(Collectors.toMap(Map.Entry::getKey,
                        e -> e.getValue().getDeviceName() == null || e.getValue().getDeviceName().isBlank()
                                ? e.getValue().getName() : e.getValue().getDeviceName()));

        List<Map<String, Object>> filtered = new ArrayList<>();
        // 全量健康分布统计（不随过滤/分页变化，供页头徽章行）
        Map<String, Long> healthStats = new LinkedHashMap<>();
        for (String h : new String[]{"online", "recently_lost", "offline", "about_to_gc"}) {
            healthStats.put(h, 0L);
        }
        for (AgentRuntimeRo r : runtimes) {
            String h = healthService.engineHealth(r.getStatus(), r.getLastSeenAt());
            healthStats.merge(h, 1L, Long::sum);
        }
        for (AgentRuntimeRo r : runtimes) {
            RuntimeCredentialRo cred = r.getCredentialId() == null ? null : credentialById.get(r.getCredentialId());
            if (scope != null && !scope.isBlank()
                    && (cred == null || !scope.equals(cred.getScope()))) continue;
            String health = healthService.engineHealth(r.getStatus(), r.getLastSeenAt());
            if (type != null && !type.isBlank() && !type.equals(r.getName())) continue;
            String devName = deviceNameByCredential.getOrDefault(r.getCredentialId(), "");
            if (device != null && !device.isBlank() && !devName.toLowerCase().contains(device.toLowerCase())) continue;
            if (status != null && !status.isBlank() && !status.equals(health)) continue;
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", r.getId());
            m.put("name", r.getName());
            m.put("displayName", r.getDisplayName());
            m.put("version", r.getVersion());
            m.put("binary", r.getBinary());
            m.put("provider", r.getProvider());
            m.put("device", devName);
            m.put("credentialId", r.getCredentialId());
            m.put("health", health);
            m.put("status", r.getStatus());
            m.put("healthy", r.getHealthy());
            m.put("lastSeenAt", r.getLastSeenAt());
            m.put("scope", r.getScope());
            m.put("ownerId", r.getOwnerId());
            filtered.add(m);
        }
        // 页头统计随过滤结果变化（与列表保持一致）
        Map<String, Long> typeCounts = filtered.stream()
                .map(m -> (String) m.get("name"))
                .collect(Collectors.groupingBy(n -> n == null ? "(unknown)" : n, Collectors.counting()));
        // 默认按最后心跳降序（最近活跃优先，null 排最后）
        filtered.sort((a, b) -> {
            String sa = (String) a.get("lastSeenAt");
            String sb = (String) b.get("lastSeenAt");
            if (sa == null && sb == null) return 0;
            if (sa == null) return 1;
            if (sb == null) return -1;
            return sb.compareTo(sa);
        });

        int total = filtered.size();
        int from = Math.min(page * size, total);
        int to = Math.min(from + size, total);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("stats", typeCounts);
        result.put("healthStats", healthStats);
        result.put("content", filtered.subList(from, to));
        result.put("totalElements", total);
        result.put("totalPages", size > 0 ? (total + size - 1) / size : 0);
        result.put("page", page);
        result.put("size", size);
        return result;
    }

    public List<Map<String, Object>> listTasks(Integer deviceId, String status, int limit) {
        List<AgentTaskRo> tasks = taskRepository.findAll();
        if (deviceId != null) {
            Set<Integer> runtimeIds = runtimeRepository.findByCredentialId(deviceId).stream()
                    .map(AgentRuntimeRo::getId).collect(Collectors.toSet());
            tasks = tasks.stream().filter(t -> t.getRuntimeId() != null && runtimeIds.contains(t.getRuntimeId()))
                    .collect(Collectors.toList());
        }
        if (status != null && !status.isBlank()) {
            tasks = tasks.stream().filter(t -> status.equals(t.getStatus())).collect(Collectors.toList());
        }
        return tasks.stream()
                .sorted((a, b) -> String.valueOf(b.getCreatedAt()).compareTo(String.valueOf(a.getCreatedAt())))
                .limit(Math.min(limit, 200))
                .map(this::toTaskMap)
                .collect(Collectors.toList());
    }

    private Map<String, Object> toTaskMap(AgentTaskRo t) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", t.getId());
        m.put("runtimeId", t.getRuntimeId());
        m.put("agentType", t.getAgentType());
        m.put("skill", t.getSkill());
        m.put("status", t.getStatus());
        m.put("exitCode", t.getExitCode());
        m.put("createdAt", t.getCreatedAt());
        m.put("completedAt", t.getCompletedAt());
        m.put("repoKey", t.getRepoKey());
        return m;
    }

    public static class NotFoundException extends RuntimeException {
        public NotFoundException(String message) {
            super(message);
        }
    }
}
