package com.grape.admin.repository.readonly;

import com.grape.admin.entity.readonly.AgentRuntimeRo;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface AgentRuntimeRoRepository extends JpaRepository<AgentRuntimeRo, Integer> {

    List<AgentRuntimeRo> findByCredentialId(Integer credentialId);
}
