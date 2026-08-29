package com.grape.admin.controller;

import com.grape.admin.config.AuthContext;
import com.grape.admin.service.RuntimeCleanupService;
import com.grape.admin.service.RuntimeCredentialService;
import com.grape.admin.service.RuntimeDeviceService;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import javax.servlet.http.HttpServletRequest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 运行时管理（设备/引擎/凭据/任务）。直读共享库；凭据与清理为受控写操作（审计）。
 */
@RestController
@RequestMapping("/api/admin")
public class RuntimeController {

    private final RuntimeDeviceService deviceService;
    private final RuntimeCredentialService credentialService;
    private final RuntimeCleanupService cleanupService;
    private final JdbcTemplate grapeJdbc;

    public RuntimeController(RuntimeDeviceService deviceService,
                             RuntimeCredentialService credentialService,
                             RuntimeCleanupService cleanupService,
                             @Qualifier("grapeJdbcTemplate") JdbcTemplate grapeJdbc) {
        this.deviceService = deviceService;
        this.credentialService = credentialService;
        this.cleanupService = cleanupService;
        this.grapeJdbc = grapeJdbc;
    }

    @GetMapping("/devices")
    public Map<String, Object> devices(@RequestParam(required = false) String scope,
                                       @RequestParam(required = false) String status,
                                       @RequestParam(required = false) String health,
                                       @RequestParam(required = false) String name,
                                       @RequestParam(defaultValue = "0") int page,
                                       @RequestParam(defaultValue = "10") int size) {
        return deviceService.listDevices(scope, status, health, name, page, size);
    }

    @GetMapping("/devices/{cid}")
    public Map<String, Object> deviceDetail(@PathVariable Integer cid,
                                            @RequestParam(defaultValue = "0") int taskPage,
                                            @RequestParam(defaultValue = "10") int taskSize,
                                            @RequestParam(required = false) Integer engineId,
                                            @RequestParam(required = false) String taskStatus,
                                            @RequestParam(required = false) String taskSkill,
                                            @RequestParam(required = false) String taskType,
                                            @RequestParam(required = false) String sortOrder) {
        return deviceService.deviceDetail(cid, taskPage, taskSize, engineId, taskStatus, taskSkill, taskType, sortOrder);
    }

    @GetMapping("/engines")
    public Map<String, Object> engines(@RequestParam(required = false) String scope,
                                       @RequestParam(required = false) String type,
                                       @RequestParam(required = false) String device,
                                       @RequestParam(required = false) String status,
                                       @RequestParam(required = false) String keyword,
                                       @RequestParam(defaultValue = "0") int page,
                                       @RequestParam(defaultValue = "10") int size) {
        return deviceService.engineList(scope, type, device, status, keyword, page, size);
    }

    /** 引擎详情：基本信息 + 关联设备 + 任务列表（筛选/分页）+ 会话列表 */
    @GetMapping("/engines/{id}")
    public Map<String, Object> engineDetail(@PathVariable Integer id,
                                            @RequestParam(defaultValue = "0") int taskPage,
                                            @RequestParam(defaultValue = "10") int taskSize,
                                            @RequestParam(required = false) String taskStatus,
                                            @RequestParam(required = false) String taskType,
                                            @RequestParam(required = false) String taskSkill,
                                            @RequestParam(required = false) String sortOrder) {
        return deviceService.engineDetail(id, taskPage, taskSize, taskStatus, taskType, taskSkill, sortOrder);
    }

    @GetMapping("/tasks")
    public List<Map<String, Object>> tasks(@RequestParam(required = false) Integer device,
                                           @RequestParam(required = false) String status,
                                           @RequestParam(defaultValue = "50") int limit) {
        return deviceService.listTasks(device, status, limit);
    }

    @GetMapping("/credentials")
    public Map<String, Object> credentials(@RequestParam(required = false) String scope,
                                           @RequestParam(required = false) String owner,
                                           @RequestParam(required = false) String status,
                                           @RequestParam(required = false) String health,
                                           @RequestParam(defaultValue = "0") int page,
                                           @RequestParam(defaultValue = "10") int size) {
        return deviceService.listCredentials(scope, owner, status, health, page, size);
    }

    @PostMapping("/credentials")
    public Map<String, String> issueCredential(@RequestBody Map<String, String> body, HttpServletRequest request) {
        // 契约：凭据名称字段为 name（凭据=身份，名称即凭据名）；scope 可选（缺省默认全局）
        String name = body.get("name");
        String token = credentialService.issue(name, body.get("scope"), AuthContext.username(request));
        return Map.of("token", token, "name", name == null ? "" : name);
    }

    @PostMapping("/devices/{cid}/revoke")
    public Map<String, String> revoke(@PathVariable Integer cid, HttpServletRequest request) {
        credentialService.revoke(cid, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @PostMapping("/credentials/{cid}/revoke")
    public Map<String, String> revokeCredential(@PathVariable Integer cid, HttpServletRequest request) {
        // 凭据=设备（1:1 共享主键），凭据吊销走凭据路径（设备页仍用 /devices/{cid}/revoke）
        credentialService.revoke(cid, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @PostMapping("/devices/{cid}/cleanup")
    public Map<String, String> cleanupDevice(@PathVariable Integer cid, HttpServletRequest request) {
        cleanupService.cleanupDevice(cid, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @DeleteMapping("/devices/{cid}")
    public Map<String, String> deleteDevice(@PathVariable Integer cid, HttpServletRequest request) {
        cleanupService.deleteDevice(cid, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @DeleteMapping("/credentials/{cid}")
    public Map<String, String> deleteCredential(@PathVariable Integer cid, HttpServletRequest request) {
        // 凭据=设备（1:1 共享主键），复用同一删除逻辑（仅 revoked 可删 + 物理清理）
        cleanupService.deleteDevice(cid, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @DeleteMapping("/engines/{id}")
    public Map<String, String> deleteEngine(@PathVariable Integer id, HttpServletRequest request) {
        cleanupService.deleteEngine(id, AuthContext.username(request));
        return Map.of("status", "ok");
    }

    @PostMapping("/tasks/cleanup")
    public Map<String, String> cleanupTasks(@RequestBody Map<String, Object> body, HttpServletRequest request) {
        @SuppressWarnings("unchecked")
        List<String> statuses = (List<String>) body.get("statuses");
        cleanupService.cleanupTasks(statuses, (String) body.get("before"), AuthContext.username(request));
        return Map.of("status", "ok");
    }

    // ===== 引擎详情任务列表：独立 API（不耦合健康中心）=====

    /** 按 Issue 获取事件列表（用于时序图 + 事件列表） */
    @GetMapping("/runtime/issues/{issueId}/events")
    public Map<String, Object> issueEvents(@PathVariable String issueId,
                                           @RequestParam(required = false) String repoKey) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("issueId", issueId);

        List<Map<String, Object>> events = new ArrayList<>();
        try {
            // 构建 SQL（repoKey 可选过滤，issueId 不唯一）
            StringBuilder taskSql = new StringBuilder("SELECT event_seq, id, runtime_id FROM agent_tasks WHERE issue_id=?");
            StringBuilder eventSql = new StringBuilder(
                "SELECT e.seq, e.event_type, e.issue_id, e.agent_type, e.skill, e.status, e.duration, e.created_at, e.actor, e.repo_key, "
                + "t.id as task_id, t.runtime_id, ar.name as engine_name, "
                + "COALESCE(rc.device_name, '本机直连') as engine_device, COALESCE(rc.last_ip, '127.0.0.1') as engine_ip "
                + "FROM events e "
                + "LEFT JOIN agent_tasks t ON t.event_seq = e.seq AND t.repo_key = e.repo_key "
                + "LEFT JOIN agent_runtimes ar ON t.runtime_id = ar.id "
                + "LEFT JOIN runtime_credentials rc ON ar.credential_id = rc.id "
                + "WHERE e.issue_id=?");
            List<Object> params = new ArrayList<>();
            params.add(issueId);
            if (repoKey != null && !repoKey.isEmpty()) {
                taskSql.append(" AND repo_key=?");
                eventSql.append(" AND e.repo_key=?");
                params.add(repoKey);
            }
            eventSql.append(" ORDER BY e.created_at");

            // 查 agent_tasks 建立 (event_seq) → task_id 映射
            Map<Integer, String> seqToTaskId = new HashMap<>();
            grapeJdbc.query(taskSql.toString(),
                    rs -> { seqToTaskId.put(rs.getInt("event_seq"), rs.getString("id")); }, params.toArray());

            grapeJdbc.query(eventSql.toString(),
                    rs -> {
                        Map<String, Object> s = new LinkedHashMap<>();
                        int seq = rs.getInt("seq");
                        s.put("seq", seq);
                        s.put("eventType", rs.getString("event_type"));
                        s.put("agentType", rs.getString("agent_type"));
                        s.put("skill", rs.getString("skill"));
                        s.put("status", rs.getString("status"));
                        s.put("duration", rs.getString("duration"));
                        s.put("createdAt", rs.getString("created_at"));
                        s.put("actor", rs.getString("actor"));
                        s.put("repoKey", rs.getString("repo_key"));
                        s.put("taskId", rs.getString("task_id") != null ? rs.getString("task_id") : seqToTaskId.getOrDefault(seq, ""));
                        // 引擎名（格式: engine-device(ip)，对齐健康中心）
                        String engineName = rs.getString("engine_name");
                        String engineDevice = rs.getString("engine_device");
                        String engineIp = rs.getString("engine_ip");
                        s.put("engine", engineName != null ? engineName + "-" + engineDevice + "(" + engineIp + ")" : "");
                        s.put("engineIp", engineIp);
                        events.add(s);
                    }, params.toArray());
        } catch (Exception e) { /* ignore */ }
        result.put("events", events);
        return result;
    }

    /** 任务日志消息 */
    @GetMapping("/runtime/tasks/{taskId}/messages")
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
                        m.put("createdAt", rs.getString("created_at"));
                        list.add(m);
                    }, taskId);
        } catch (Exception e) { /* ignore */ }
        return list;
    }

    /** 任务详情（含时间线信息） */
    @GetMapping("/runtime/tasks/{taskId}/info")
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
}
