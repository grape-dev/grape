package com.grape.admin.repository;

import com.grape.admin.entity.AdminUserRoleRel;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface AdminUserRoleRelRepository extends JpaRepository<AdminUserRoleRel, Long> {

    List<AdminUserRoleRel> findByUserId(Long userId);

    List<AdminUserRoleRel> findByUserIdIn(java.util.Collection<Long> userIds);

    boolean existsByRoleId(Long roleId);

    @Modifying
    @Query("update AdminUserRoleRel r set r.deleted = 1, r.updatedBy = :updatedBy where r.userId = :userId and r.deleted = 0")
    int softDeleteByUserId(@Param("userId") Long userId, @Param("updatedBy") String updatedBy);

    /** 有效绑定按角色统计用户数（角色分布徽章） */
    @Query("select rel.roleId, count(rel) from AdminUserRoleRel rel where rel.deleted = 0 group by rel.roleId")
    List<Object[]> countActiveByRoleId();
}
