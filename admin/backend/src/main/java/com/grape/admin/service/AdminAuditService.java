package com.grape.admin.service;

import com.grape.admin.entity.AdminAuditLog;
import com.grape.admin.repository.AdminAuditLogRepository;
import org.springframework.stereotype.Service;

@Service
public class AdminAuditService {

    private final AdminAuditLogRepository auditLogRepository;

    public AdminAuditService(AdminAuditLogRepository auditLogRepository) {
        this.auditLogRepository = auditLogRepository;
    }

    public void record(Long userId, String username, String action, String target, String detail) {
        AdminAuditLog log = new AdminAuditLog();
        log.setUserId(userId);
        log.setUsername(username);
        log.setAction(action);
        log.setTarget(target);
        log.setDetail(detail == null ? "" : detail);
        auditLogRepository.save(log);
    }
}
