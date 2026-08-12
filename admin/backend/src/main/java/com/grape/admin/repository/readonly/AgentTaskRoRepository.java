package com.grape.admin.repository.readonly;

import com.grape.admin.entity.readonly.AgentTaskRo;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

import java.util.List;

public interface AgentTaskRoRepository extends JpaRepository<AgentTaskRo, String> {

    List<AgentTaskRo> findTop50ByRuntimeIdOrderByCreatedAtDesc(Integer runtimeId);

    /** 每个智能体的任务总数：SELECT agent_type, COUNT(*) ... GROUP BY agent_type */
    @Query("SELECT t.agentType, COUNT(t) FROM AgentTaskRo t GROUP BY t.agentType")
    List<Object[]> countByAgentType();

    /** 每个智能体 × 任务状态的分布：SELECT agent_type, status, COUNT(*) ... GROUP BY agent_type, status */
    @Query("SELECT t.agentType, t.status, COUNT(t) FROM AgentTaskRo t GROUP BY t.agentType, t.status")
    List<Object[]> countByAgentTypeAndStatus();
}
