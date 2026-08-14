package com.grape.admin.repository.readonly;

import com.grape.admin.entity.readonly.RuntimeCredentialRo;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.Optional;

public interface RuntimeCredentialRoRepository extends JpaRepository<RuntimeCredentialRo, Integer> {

    Optional<RuntimeCredentialRo> findById(Integer id);
}
