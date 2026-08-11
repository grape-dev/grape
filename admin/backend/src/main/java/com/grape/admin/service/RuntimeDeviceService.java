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

    public List<Map<String, Object>> listCredentials(String scope, String owner) {
        List<RuntimeCredentialRo> credentials = credentialRepository.findAll();
        List<AgentRuntimeRo> runtimes = runtimeRepository.findAll();
        Map<Integer, Long> engineCountByCredential = runtimes.stream()
                .filter(r -> r.getCredentialId() != null)
                .collect(Collectors.groupingBy(AgentRuntimeRo::getCredentialId, Collectors.counting()));
        return credentials.stream()
                .filter(c -> scope == null || scope.isBlank() || scope.equals(c.getScope()))
                .filter(c -> owner == null || owner.isBlank()
                        || (c.getOwnerName() != null && c.getOwnerName().toLowerCase().contains(owner.toLowerCase())))
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
    }

    public List<Map<String, Object>> listDevices() {
        List<RuntimeCredentialRo> credentials = credentialRepository.findAll();
        List<AgentRuntimeRo> runtimes = runtimeRepository.findAll();
        Map<Integer, List<AgentRuntimeRo>> enginesByCredential = runtimes.stream()
                .filter(r -> r.getCredentialId() != null)
                .collect(Collectors.groupingBy(AgentRuntimeRo::getCredentialId));
        Map<Integer, Long> taskCountByRuntime = taskRepository.findAll().stream()
                .filter(t -> t.getRuntimeId() != null)
                .collect(Collectors.groupingBy(AgentTaskRo::getRuntimeId, Collectors.counting()));

        List<Map<String, Object>> devices = new ArrayList<>();
        for (RuntimeCredentialRo c : credentials) {
            List<AgentRuntimeRo> engines = enginesByCredential.getOrDefault(c.getId(), List.of());
            long taskCount = engines.stream()
                    .mapToLong(e -> taskCountByRuntime.getOrDefault(e.getId(), 0L)).sum();
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("id", c.getId());
            d.put("name", c.getName());
            d.put("deviceName", c.getDeviceName());
            d.put("status", c.getStatus());
            d.put("health", healthService.deviceHealth(c.getStatus(), c.getLastSeenAt()));
            d.put("lastSeenAt", c.getLastSeenAt());
            d.put("lastIp", c.getLastIp());
            d.put("engineCount", engines.size());
            d.put("taskCount", taskCount);
            devices.add(d);
        }
        return devices;
    }

    public Map<String, Object> deviceDetail(Integer credentialId) {
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
        List<AgentTaskRo> recentTasks = runtimeIds.isEmpty() ? List.of()
                : taskRepository.findAll().stream()
                        .filter(t -> t.getRuntimeId() != null && runtimeIds.contains(t.getRuntimeId()))
                        .sorted((a, b) -> String.valueOf(b.getCreatedAt()).compareTo(String.valueOf(a.getCreatedAt())))
                        .limit(50)
                        .collect(Collectors.toList());

        device.put("engines", engineList);
        device.put("tasks", recentTasks.stream().map(this::toTaskMap).collect(Collectors.toList()));
        return device;
    }

    public List<Map<String, Object>> engineOverview() {
        List<AgentRuntimeRo> runtimes = runtimeRepository.findAll();
        Map<String, List<AgentRuntimeRo>> byName = runtimes.stream()
                .collect(Collectors.groupingBy(r -> r.getName() == null ? "(unknown)" : r.getName()));
        List<Map<String, Object>> result = new ArrayList<>();
        for (Map.Entry<String, List<AgentRuntimeRo>> e : byName.entrySet()) {
            List<AgentRuntimeRo> list = e.getValue();
            Map<String, Long> healthCounts = list.stream()
                    .collect(Collectors.groupingBy(
                            r -> healthService.engineHealth(r.getStatus(), r.getLastSeenAt()),
                            Collectors.counting()));
            Map<String, Long> versionCounts = list.stream()
                    .collect(Collectors.groupingBy(r -> r.getVersion() == null ? "(未知)" : r.getVersion(),
                            Collectors.counting()));
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", e.getKey());
            m.put("binary", list.get(0).getBinary());
            m.put("total", list.size());
            m.put("health", healthCounts);
            m.put("versions", versionCounts);
            result.add(m);
        }
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
