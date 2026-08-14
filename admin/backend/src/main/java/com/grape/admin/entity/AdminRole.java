package com.grape.admin.entity;

import lombok.Getter;
import lombok.Setter;
import org.hibernate.annotations.Where;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.GeneratedValue;
import javax.persistence.GenerationType;
import javax.persistence.Id;
import javax.persistence.Table;

@Getter
@Setter
@Entity
@Table(name = "admin_roles")
@Where(clause = "deleted = 0")
public class AdminRole extends AdminAuditBase {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    /** SUPER_ADMIN / VIEWER / OPS / 自定义 */
    @Column(nullable = false)
    private String code;

    @Column(nullable = false)
    private String name;

    /** 1 保护角色（不可改删） */
    @Column(name = "is_protected")
    private Integer isProtected;
}
