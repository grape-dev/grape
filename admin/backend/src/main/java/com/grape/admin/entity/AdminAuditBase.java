package com.grape.admin.entity;

import lombok.Getter;
import lombok.Setter;

import javax.persistence.Column;
import javax.persistence.MappedSuperclass;
import java.time.LocalDateTime;

/**
 * admin_* 表公共审计字段（五件套）：创建时间/创建人/更新时间/更新人/软删除标记。
 * updated_at 由 MySQL ON UPDATE CURRENT_TIMESTAMP 自动维护；created_by/updated_by 由 Service 层从 JWT 上下文填充。
 */
@Getter
@Setter
@MappedSuperclass
public abstract class AdminAuditBase {

    @Column(name = "created_at", insertable = false, updatable = false)
    private LocalDateTime createdAt;

    @Column(name = "created_by")
    private String createdBy;

    @Column(name = "updated_at", insertable = false, updatable = false)
    private LocalDateTime updatedAt;

    @Column(name = "updated_by")
    private String updatedBy;

    @Column(name = "deleted", insertable = false)
    private Integer deleted;
}
