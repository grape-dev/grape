package com.grape.admin.entity;

import lombok.Getter;
import lombok.Setter;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;
import java.time.LocalDateTime;

/** AI 助手会话（admin 运营管理台——会话 id = session_id UUID——绑定 hermes --continue） */
@Getter
@Setter
@Entity
@Table(name = "admin_ai_chat_session")
@org.hibernate.annotations.Where(clause = "deleted = 0")
public class AdminAiChatSession {

    @Id
    @Column(name = "session_id", nullable = false, length = 64)
    private String sessionId;

    @Column(nullable = false, length = 100)
    private String name = "新对话";

    @Column(name = "created_by", nullable = false, length = 64)
    private String createdBy;

    @Column(name = "created_at")
    private LocalDateTime createdAt = LocalDateTime.now();

    @Column(name = "updated_at")
    private LocalDateTime updatedAt = LocalDateTime.now();

    @Column
    private Integer deleted = 0;
}
