package com.grape.admin.repository.readonly;

import com.grape.admin.entity.readonly.AgentTaskRo;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface AgentTaskRoRepository extends JpaRepository<AgentTaskRo, String> {

    List<AgentTaskRo> findTop50ByRuntimeIdOrderByCreatedAtDesc(Integer runtimeId);
}
