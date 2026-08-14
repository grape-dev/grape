package com.grape.admin.repository.readonly;

import com.grape.admin.entity.readonly.ManagedRepoRo;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ManagedRepoRoRepository extends JpaRepository<ManagedRepoRo, String> {
}
