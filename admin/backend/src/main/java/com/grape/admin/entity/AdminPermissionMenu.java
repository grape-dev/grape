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
@Table(name = "admin_permission_menus")
@Where(clause = "deleted = 0")
public class AdminPermissionMenu extends AdminAuditBase {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    /** runtime.devices / runtime.engines / runtime.credentials / ai.assistant / sys.users / sys.roles */
    @Column(nullable = false)
    private String code;

    @Column(nullable = false)
    private String name;

    /** 前端路由，如 /runtime/devices；父级菜单为空串 */
    @Column(nullable = false)
    private String path;

    @Column(name = "parent_id")
    private Long parentId;

    private Integer sort;
}
