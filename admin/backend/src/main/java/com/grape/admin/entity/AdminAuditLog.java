package com.grape.admin.entity;

import lombok.Getter;
import lombok.Setter;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.GeneratedValue;
import javax.persistence.GenerationType;
import javax.persistence.Id;
import javax.persistence.Table;
import java.time.LocalDateTime;

/**
 * 操作审计日志（append-only，无更新/删除语义，不继承 AdminAuditBase）。
 */
@Getter
@Setter
@Entity
@Table(name = "admin_audit_log")
public class AdminAuditLog {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "user_id")
    private Long userId;

    private String username;

    /** credential.issue / credential.revoke / device.cleanup / engine.delete / task.cleanup / user.* / role.* */
    @Column(nullable = false)
    private String action;

    private String target;

    private String detail;

    @Column(name = "created_at", insertable = false, updatable = false)
    private LocalDateTime createdAt;
}
