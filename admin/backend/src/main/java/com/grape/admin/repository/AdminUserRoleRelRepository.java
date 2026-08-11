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
}
