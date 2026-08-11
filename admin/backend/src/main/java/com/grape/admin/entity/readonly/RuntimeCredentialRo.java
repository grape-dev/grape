package com.grape.admin.entity.readonly;

import lombok.Getter;
import lombok.Setter;
import org.hibernate.annotations.Immutable;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

/**
 * 共享表 runtime_credentials（grape 的 worker 凭据表）——只读。
 * 契约：token_hash = sha256(raw_token) 小写 hex；status ∈ {active, revoked}。
 */
@Getter
@Setter
@Entity
@Immutable
@Table(name = "runtime_credentials")
public class RuntimeCredentialRo {

    @Id
    private Integer id;

    @Column(name = "token_hash")
    private String tokenHash;

    private String name;

    @Column(name = "device_name")
    private String deviceName;

    private String scope;

    @Column(name = "owner_id")
    private String ownerId;

    @Column(name = "owner_name")
    private String ownerName;

    /** active / revoked（grape 契约） */
    private String status;

    @Column(name = "last_seen_at")
    private String lastSeenAt;

    @Column(name = "last_ip")
    private String lastIp;

    @Column(name = "created_at")
    private String createdAt;
}
