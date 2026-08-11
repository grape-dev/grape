package com.grape.admin.controller;

import com.grape.admin.service.AiAssistantService;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.Map;

/**
 * AI 助手（SSE 流式）：POST /api/admin/ai/chat {query, sessionId?}
 */
@RestController
@RequestMapping("/api/admin/ai")
public class AiController {

    private final AiAssistantService aiAssistantService;

    public AiController(AiAssistantService aiAssistantService) {
        this.aiAssistantService = aiAssistantService;
    }

    @PostMapping(value = "/chat", produces = "text/event-stream")
    public SseEmitter chat(@RequestBody Map<String, String> body) {
        SseEmitter emitter = new SseEmitter(180_000L);
        aiAssistantService.stream(body.getOrDefault("query", ""), body.get("sessionId"), emitter);
        return emitter;
    }
}
