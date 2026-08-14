package com.grape.admin.entity.readonly;

import lombok.Getter;
import lombok.Setter;
import org.hibernate.annotations.Immutable;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

/**
 * 共享表 agent_tasks（任务队列，runtime_id 关联引擎/设备）——只读。
 * 状态枚举（grape 契约）：queued / dispatched / running / completed / failed / cancelled。
 */
@Getter
@Setter
@Entity
@Immutable
@Table(name = "agent_tasks")
public class AgentTaskRo {

    @Id
    private String id;

    @Column(name = "agent_session_id")
    private String agentSessionId;

    @Column(name = "runtime_id")
    private Integer runtimeId;

    @Column(name = "event_seq")
    private Integer eventSeq;

    @Column(name = "issue_id")
    private String issueId;

    @Column(name = "repo_key")
    private String repoKey;

    @Column(name = "agent_type")
    private String agentType;

    private String skill;

    private String prompt;

    private String cwd;

    @Column(name = "runtime_session_id")
    private String runtimeSessionId;

    @Column(name = "event_type")
    private String eventType;

    private String account;

    @Column(name = "user_id")
    private Integer userId;

    private String status;

    @Column(name = "exit_code")
    private Integer exitCode;

    @Column(name = "result_text")
    private String resultText;

    private String errors;

    private String stderr;

    @Column(name = "usage_json")
    private String usageJson;

    @Column(name = "started_at")
    private String startedAt;

    @Column(name = "completed_at")
    private String completedAt;

    @Column(name = "created_at")
    private String createdAt;
}
