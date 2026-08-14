package com.grape.admin.entity.readonly;

import javax.persistence.Column;
import javax.persistence.Entity;
import javax.persistence.Id;
import javax.persistence.Table;

/**
 * 只读映射 grape 运行库 agent_type 表（智能体定义，由 grape 服务端种子/维护）。
 * 字段对齐 Python src/api/db_setup.py 的 agent_type DDL。
 */
@Entity
@Table(name = "agent_type")
public class AgentTypeRo {

    @Id
    @Column(name = "agent_type")
    private String agentType;

    @Column(name = "display_name")
    private String displayName;

    private String description;

    @Column(name = "default_runtime_id")
    private Integer defaultRuntimeId;

    private String icon;

    @Column(name = "order_num")
    private Integer orderNum;

    @Column(name = "subscribe_events")
    private String subscribeEvents;

    private Integer enable;

    public String getAgentType() { return agentType; }
    public void setAgentType(String agentType) { this.agentType = agentType; }
    public String getDisplayName() { return displayName; }
    public void setDisplayName(String displayName) { this.displayName = displayName; }
    public String getDescription() { return description; }
    public void setDescription(String description) { this.description = description; }
    public Integer getDefaultRuntimeId() { return defaultRuntimeId; }
    public void setDefaultRuntimeId(Integer defaultRuntimeId) { this.defaultRuntimeId = defaultRuntimeId; }
    public String getIcon() { return icon; }
    public void setIcon(String icon) { this.icon = icon; }
    public Integer getOrderNum() { return orderNum; }
    public void setOrderNum(Integer orderNum) { this.orderNum = orderNum; }
    public String getSubscribeEvents() { return subscribeEvents; }
    public void setSubscribeEvents(String subscribeEvents) { this.subscribeEvents = subscribeEvents; }
    public Integer getEnable() { return enable; }
    public void setEnable(Integer enable) { this.enable = enable; }
}
