package com.grape.admin.repository;

import com.grape.admin.entity.AdminPermissionMenu;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface AdminPermissionMenuRepository extends JpaRepository<AdminPermissionMenu, Long> {

    List<AdminPermissionMenu> findAllByOrderBySortAsc();
}
