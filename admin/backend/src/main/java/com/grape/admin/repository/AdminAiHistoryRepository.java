package com.grape.admin.repository;

import com.grape.admin.entity.AdminAiHistory;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;

public interface AdminAiHistoryRepository extends JpaRepository<AdminAiHistory, Long> {

    Page<AdminAiHistory> findByQueryContainingIgnoreCase(String keyword, Pageable pageable);

    Page<AdminAiHistory> findAllByOrderByCreatedAtDesc(Pageable pageable);

    Optional<AdminAiHistory> findByTaskId(String taskId);
}
