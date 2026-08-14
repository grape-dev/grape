package com.grape.admin.entity.readonly;

import lombok.Getter;
import lombok.Setter;
import org.hibernate.annotations.Immutable;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

/**
 * 共享表 managed_repos（已托管的代码仓）——只读。
 * 注意：bot_gitcode_token / gitcode_webhook_token 为敏感凭据，不映射、不外泄。
 */
@Getter
@Setter
@Entity
@Immutable
@Table(name = "managed_repos")
public class ManagedRepoRo {

    @Id
    @Column(name = "repo_key")
    private String repoKey;

    @Column(name = "repo_owner")
    private String repoOwner;

    @Column(name = "repo_name")
    private String repoName;

    @Column(name = "full_name")
    private String fullName;

    @Column(name = "webhook_id")
    private Integer webhookId;

    @Column(name = "dev_scene")
    private String devScene;

    @Column(name = "bot_username")
    private String botUsername;

    @Column(name = "created_at")
    private String createdAt;

    @Column(name = "is_admin")
    private Integer isAdmin;
}
