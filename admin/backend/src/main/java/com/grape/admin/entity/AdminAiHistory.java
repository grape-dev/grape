package com.grape.admin.entity;

import lombok.Getter;
import lombok.Setter;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.GeneratedValue;
import javax.persistence.GenerationType;
import javax.persistence.Id;
import javax.persistence.Table;
import java.time.LocalDateTime;

/** AI 助手问答历史（自有表，软删除） */
@Getter
@Setter
@Entity
@Table(name = "admin_ai_history")
@org.hibernate.annotations.Where(clause = "deleted = 0")
public class AdminAiHistory {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "session_id", nullable = false, length = 64)
    private String sessionId;

    @Column(name = "name", length = 100)
    private String name;

    @Column(name = "task_id", nullable = false, length = 64)
    private String taskId;

    @Column(nullable = false, columnDefinition = "TEXT")
    private String query;

    @Column(nullable = false, columnDefinition = "TEXT")
    private String answer;

    @Column(name = "created_at")
    private LocalDateTime createdAt = LocalDateTime.now();

    @Column
    private Integer deleted = 0;
}
