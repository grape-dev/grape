package com.grape.admin.repository;

import com.grape.admin.entity.AdminRoleMenuRel;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface AdminRoleMenuRelRepository extends JpaRepository<AdminRoleMenuRel, Long> {

    List<AdminRoleMenuRel> findByRoleId(Long roleId);

    /** 软删除某角色的全部当前绑定（权限保存 = 旧绑定标 deleted=1 + 新集合插入 deleted=0） */
    @Modifying
    @Query("update AdminRoleMenuRel r set r.deleted = 1, r.updatedBy = :updatedBy where r.roleId = :roleId and r.deleted = 0")
    int softDeleteByRoleId(@Param("roleId") Long roleId, @Param("updatedBy") String updatedBy);

    @Modifying
    @Query("update AdminRoleMenuRel r set r.deleted = 1, r.updatedBy = :updatedBy where r.menuId = :menuId and r.deleted = 0")
    int softDeleteByMenuId(@Param("menuId") Long menuId, @Param("updatedBy") String updatedBy);
}
