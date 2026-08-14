package com.grape.admin.repository;

import com.grape.admin.entity.AdminRole;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface AdminRoleRepository extends JpaRepository<AdminRole, Long> {

    Optional<AdminRole> findByCode(String code);

    List<AdminRole> findAllByOrderByIdAsc();
}
