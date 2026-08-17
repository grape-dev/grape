package com.grape.admin.repository;

import com.grape.admin.entity.AdminAiChatSession;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.List;

public interface AdminAiChatSessionRepository extends JpaRepository<AdminAiChatSession, String> {

    List<AdminAiChatSession> findByCreatedByOrderByUpdatedAtDesc(String createdBy, Pageable pageable);

    /** 游标分页（updated_at + session_id 复合游标——禁 offset——排序一致） */
    @Query("select s from AdminAiChatSession s where s.createdBy = :user and s.deleted = 0 "
            + "and (:cursorTime is null or s.updatedAt < :cursorTime "
            + "or (s.updatedAt = :cursorTime and s.sessionId < :cursorId)) "
            + "order by s.updatedAt desc, s.sessionId desc")
    List<AdminAiChatSession> findPageByCreatedBy(@Param("user") String user,
            @Param("cursorTime") java.time.LocalDateTime cursorTime,
            @Param("cursorId") String cursorId, Pageable pageable);

    long countByCreatedBy(String createdBy);

    @Query("select count(s) from AdminAiChatSession s where s.createdBy = :user and s.deleted = 0")
    long countByCreatedByAndDeleted(@Param("user") String user);
}
