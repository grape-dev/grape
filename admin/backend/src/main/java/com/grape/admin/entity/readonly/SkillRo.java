package com.grape.admin.entity.readonly;

import lombok.Getter;
import lombok.Setter;
import org.hibernate.annotations.Immutable;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

/**
 * 共享表 skill（grape 技能库，官方/仓库/个人三态）——只读。
 */
@Getter
@Setter
@Entity
@Immutable
@Table(name = "skill")
public class SkillRo {

    @Id
    private Integer id;

    @Column(name = "skill_name")
    private String skillName;

    @Column(name = "display_name")
    private String displayName;

    private String description;

    @Column(name = "agent_type")
    private String agentType;

    @Column(name = "skill_path")
    private String skillPath;

    private String scope;

    @Column(name = "owner_id")
    private String ownerId;

    @Column(name = "is_active")
    private Integer isActive;

    @Column(name = "created_at")
    private String createdAt;

    @Column(name = "updated_at")
    private String updatedAt;
}
