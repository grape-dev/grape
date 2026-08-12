package com.grape.admin.controller;

import com.grape.admin.config.AuthContext;
import com.grape.admin.service.AgentService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import javax.servlet.http.HttpServletRequest;
import java.util.Map;

@RestController
@RequestMapping("/api/admin")
public class AgentController {

    private final AgentService agentService;

    public AgentController(AgentService agentService) {
        this.agentService = agentService;
    }

    /** 智能体列表 + 任务聚合（卡片墙 + 底部任务分布图的数据源） */
    @GetMapping("/agents")
    public Map<String, Object> agents() {
        return agentService.list();
    }

    /** 智能体详情（卡片点击进入）：基本 + 指标 + 仓库明细 + 任务分布 + 预警值 */
    @GetMapping("/agents/{type}/detail")
    public Map<String, Object> detail(@PathVariable String type) {
        return agentService.detail(type);
    }

    /** 设置负载预警阈值：POST /api/admin/agents/{type}/alert  body: {"thresholdPct": 80} */
    @PostMapping("/agents/{type}/alert")
    public Map<String, Object> setAlert(@PathVariable String type,
                                        @RequestBody Map<String, Object> body,
                                        HttpServletRequest request) {
        Object raw = body.get("thresholdPct");
        int thresholdPct = raw instanceof Number ? ((Number) raw).intValue()
                : Integer.parseInt(String.valueOf(raw));
        int saved = agentService.setAlertThreshold(type, thresholdPct, AuthContext.username(request));
        return Map.of("agentType", type, "alertThresholdPct", saved);
    }

    /** 启用/禁用智能体：POST /api/admin/agents/{type}/enable  body: {"enable": true|false} */
    @PostMapping("/agents/{type}/enable")
    public Map<String, Object> toggleEnable(@PathVariable String type,
                                            @RequestBody Map<String, Object> body,
                                            HttpServletRequest request) {
        boolean enable = Boolean.TRUE.equals(body.get("enable"));
        Integer updated = agentService.toggleEnable(type, enable, AuthContext.username(request));
        return Map.of("agentType", type, "enable", updated);
    }
}
