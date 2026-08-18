package com.grape.admin.repository;

import com.grape.admin.entity.AdminAiChatMessage;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;
import java.util.Optional;

public interface AdminAiChatMessageRepository extends JpaRepository<AdminAiChatMessage, String> {

    /** 会话内 seq 最大值（无消息返回 0）——seq 生成（MAX+1——唯一约束兜底） */
    @Query("select coalesce(max(m.seq), 0) from AdminAiChatMessage m where m.sessionId = :sessionId and m.deleted = 0")
    long maxSeq(@Param("sessionId") String sessionId);

    /** 游标分页：取比 cursorSeq 更早的消息（倒序——limit 由外部控制） */
    @Query("select m from AdminAiChatMessage m where m.sessionId = :sessionId and m.deleted = 0 and (:cursorSeq is null or m.seq < :cursorSeq) order by m.seq desc")
    List<AdminAiChatMessage> findBySessionIdOrderBySeqDesc(@Param("sessionId") String sessionId, @Param("cursorSeq") Long cursorSeq, org.springframework.data.domain.Pageable pageable);

    /** 幂等校验：同 task_id 是否已有 assistant 消息 */
    @Query("select m from AdminAiChatMessage m where m.sessionId = :sessionId and m.taskId = :taskId and m.role = 'assistant' and m.deleted = 0")
    Optional<AdminAiChatMessage> findAssistantByTask(@Param("sessionId") String sessionId, @Param("taskId") String taskId);

    /** 查库任务状态兜底（status 接口——内存清理后查该 task_id 的消息） */
    @Query("select m from AdminAiChatMessage m where m.taskId = :taskId and m.deleted = 0 order by m.seq asc")
    List<AdminAiChatMessage> findByTaskIdOrderBySeq(@Param("taskId") String taskId);

    @Modifying
    @Query("update AdminAiChatMessage m set m.deleted = 1 where m.sessionId = :sessionId")
    void softDeleteBySession(@Param("sessionId") String sessionId);
}
