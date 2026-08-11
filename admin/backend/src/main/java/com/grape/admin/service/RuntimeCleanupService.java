package com.grape.admin.service;

import com.grape.admin.common.BizException;
import com.grape.admin.common.NotFoundException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * 运行时清理（共享表物理删除——共享表无 deleted 列）：
 * - 设备回收：吊销凭据 → 删引擎记录 → 清终态孤儿任务
 * - 引擎删除：存在进行中任务拒绝
 * - 任务清理：仅终态（completed/failed/cancelled）+ 时间范围
 * 护栏：进行中任务绝不清理；所有操作写 admin_audit_log。
 */
@Service
public class RuntimeCleanupService {

    private static final Set<String> ACTIVE_STATUSES = Set.of("queued", "dispatched", "running");
    private static final Set<String> TERMINAL_STATUSES = Set.of("completed", "failed", "cancelled");

    private final JdbcTemplate jdbc;
    private final AdminAuditService auditService;

    public RuntimeCleanupService(JdbcTemplate jdbc, AdminAuditService auditService) {
        this.jdbc = jdbc;
        this.auditService = auditService;
    }

    public void cleanupDevice(Integer credentialId, String operator) {
        List<Integer> runtimeIds = jdbc.query(
                "SELECT id FROM agent_runtimes WHERE credential_id = ?", (rs, i) -> rs.getInt(1), credentialId);
        if (!runtimeIds.isEmpty()) {
            long active = countActiveTasks(runtimeIds);
            if (active > 0) {
                throw new BizException("设备存在 " + active + " 个进行中任务，拒绝回收");
            }
        }
        int revoked = jdbc.update("UPDATE runtime_credentials SET status = 'revoked' WHERE id = ? AND status = 'active'",
                credentialId);
        if (revoked == 0) {
            throw new NotFoundException("设备不存在或已吊销: " + credentialId);
        }
        jdbc.update("DELETE FROM agent_runtimes WHERE credential_id = ?", credentialId);
        if (!runtimeIds.isEmpty()) {
            String in = inClause(runtimeIds);
            jdbc.update("DELETE FROM agent_tasks WHERE runtime_id IN (" + in + ") AND status IN "
                    + terminalIn());
        }
        auditService.record(null, operator, "device.cleanup", "device:" + credentialId,
                "runtimes=" + runtimeIds.size() + ", tasks清理完成");
    }

    public void deleteEngine(Integer runtimeId, String operator) {
        List<Integer> ids = List.of(runtimeId);
        if (countActiveTasks(ids) > 0) {
            throw new BizException("引擎存在进行中任务，拒绝删除");
        }
        int n = jdbc.update("DELETE FROM agent_runtimes WHERE id = ?", runtimeId);
        if (n == 0) {
            throw new NotFoundException("引擎不存在: " + runtimeId);
        }
        auditService.record(null, operator, "engine.delete", "runtime:" + runtimeId, "");
    }

    public void cleanupTasks(List<String> statuses, String before, String operator) {
        if (statuses == null || statuses.isEmpty()) {
            throw new BizException("必须指定要清理的状态");
        }
        for (String s : statuses) {
            if (!TERMINAL_STATUSES.contains(s)) {
                throw new BizException("只允许清理终态任务，非法状态: " + s);
            }
        }
        String in = statuses.stream().map(s -> "'" + s + "'").collect(Collectors.joining(","));
        String sql = "DELETE FROM agent_tasks WHERE status IN (" + in + ")";
        if (before != null && !before.isBlank()) {
            sql += " AND created_at < '" + before + "'";
        }
        int n = jdbc.update(sql);
        auditService.record(null, operator, "task.cleanup", "",
                "statuses=" + String.join(",", statuses) + ", before=" + before + ", deleted=" + n);
    }

    private long countActiveTasks(List<Integer> runtimeIds) {
        String in = inClause(runtimeIds);
        Integer n = jdbc.queryForObject(
                "SELECT COUNT(*) FROM agent_tasks WHERE runtime_id IN (" + in + ") AND status IN " + activeIn(),
                Integer.class);
        return n == null ? 0 : n;
    }

    private String inClause(List<Integer> ids) {
        return ids.stream().map(String::valueOf).collect(Collectors.joining(","));
    }

    private String activeIn() {
        return ACTIVE_STATUSES.stream().map(s -> "'" + s + "'").collect(Collectors.joining(",", "(", ")"));
    }

    private String terminalIn() {
        return TERMINAL_STATUSES.stream().map(s -> "'" + s + "'").collect(Collectors.joining(",", "(", ")"));
    }
}
