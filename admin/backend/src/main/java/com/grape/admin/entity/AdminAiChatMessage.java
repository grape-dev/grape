package com.grape.admin.entity;

import lombok.Getter;
import lombok.Setter;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;
import java.time.LocalDateTime;

/** AI 助手消息（每轮 user + assistant 各一条——task_id 配对——seq 递增——唯一约束兜底） */
@Getter
@Setter
@Entity
@Table(name = "admin_ai_chat_message")
@org.hibernate.annotations.Where(clause = "deleted = 0")
public class AdminAiChatMessage {

    @Id
    @Column(name = "msg_id", nullable = false, length = 64)
    private String msgId;

    @Column(name = "session_id", nullable = false, length = 64)
    private String sessionId;

    @Column(name = "task_id", length = 64)
    private String taskId;

    @Column(name = "send_status")
    private Integer sendStatus;

    @Column(name = "generate_status")
    private Integer generateStatus;

    @Column(name = "error_code", length = 32)
    private String errorCode;

    @Column(name = "error_msg", length = 512)
    private String errorMsg;

    @Column(nullable = false, length = 16)
    private String role;

    @Column(nullable = false, columnDefinition = "TEXT")
    private String content;

    @Column(nullable = false)
    private Long seq;

    @Column(name = "create_time")
    private LocalDateTime createTime = LocalDateTime.now();

    @Column
    private Integer deleted = 0;
}
