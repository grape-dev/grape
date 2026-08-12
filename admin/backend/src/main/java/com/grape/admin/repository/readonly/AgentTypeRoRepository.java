package com.grape.admin.repository.readonly;

import com.grape.admin.entity.readonly.AgentTypeRo;
import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface AgentTypeRoRepository extends JpaRepository<AgentTypeRo, String> {

    List<AgentTypeRo> findAllByOrderByOrderNumAsc();
}
