package com.grape.admin.entity.readonly;

import lombok.Getter;
import lombok.Setter;
import org.hibernate.annotations.Immutable;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

/**
 * 共享表 agent_runtimes（设备上的引擎记录，credential_id 关联设备）——只读。
 * 注意：binary 是 MySQL 保留字，列名必须反引号（grape 历史 500 bug 的教训）。
 */
@Getter
@Setter
@Entity
@Immutable
@Table(name = "agent_runtimes")
public class AgentRuntimeRo {

    @Id
    private Integer id;

    @Column(name = "credential_id")
    private Integer credentialId;

    private String name;

    @Column(name = "display_name")
    private String displayName;

    private String provider;

    @Column(name = "`binary`")
    private String binary;

    private Integer installed;

    @Column(name = "cli_path")
    private String cliPath;

    private String version;

    private Integer healthy;

    @Column(name = "pip_package")
    private String pipPackage;

    /** online / offline 等（grape 健康状态语义，见 src/runtime/health.py） */
    private String status;

    @Column(name = "last_seen_at")
    private String lastSeenAt;

    private String scope;

    @Column(name = "owner_id")
    private String ownerId;

    @Column(name = "updated_at")
    private String updatedAt;
}
