package com.grape.admin.controller;

import com.grape.admin.service.AiHistoryService;
import com.grape.admin.service.AiTaskManager;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.util.LinkedHashMap;
import java.util.Map;

@RestController
@RequestMapping("/api/admin/ai")
public class AiController {

    private final AiTaskManager taskManager;
    private final AiHistoryService historyService;

    public AiController(AiTaskManager taskManager, AiHistoryService historyService) {
        this.taskManager = taskManager;
        this.historyService = historyService;
    }

    /** 兼容直连模式：提交 + 订阅一步完成（老客户端/单请求场景） */
    @PostMapping(value = "/chat", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter chat(@RequestBody Map<String, String> body) {
        String taskId = taskManager.submit(body.getOrDefault("query", ""), body.get("sessionId"));
        SseEmitter emitter = new SseEmitter(300_000L);
        taskManager.subscribe(taskId, emitter);
        return emitter;
    }

    /** 异步提交：立即返回 taskId，不阻塞 */
    @PostMapping("/chat/submit")
    public Map<String, Object> submit(@RequestBody Map<String, String> body) {
        String taskId = taskManager.submit(body.getOrDefault("query", ""), body.get("sessionId"));
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("taskId", taskId);
        resp.put("status", "running");
        return resp;
    }

    /** 查询任务状态 */
    @GetMapping("/chat/task/{taskId}/status")
    public Map<String, Object> status(@PathVariable String taskId) {
        AiTaskManager.AiTask task = taskManager.get(taskId);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("taskId", task.taskId);
        resp.put("status", task.status);
        resp.put("error", task.error);
        resp.put("sessionId", task.hermesSessionId);
        resp.put("elapsedMs", System.currentTimeMillis() - task.createdAt);
        return resp;
    }

    /** 订阅任务输出（SSE）：先重放缓冲，再实时追加，终态自动结束 */
    @GetMapping(value = "/chat/task/{taskId}/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter stream(@PathVariable String taskId) {
        SseEmitter emitter = new SseEmitter(300_000L);
        taskManager.subscribe(taskId, emitter);
        return emitter;
    }

    /** 问答历史列表（分页 + 关键字） */
    @GetMapping("/history")
    public Map<String, Object> history(@RequestParam(required = false) String keyword,
                                       @RequestParam(defaultValue = "0") int page,
                                       @RequestParam(defaultValue = "10") int size) {
        return historyService.list(keyword, page, size);
    }

    /** 删除一条历史 */
    @DeleteMapping("/history/{id}")
    public Map<String, Object> deleteHistory(@PathVariable Long id) {
        historyService.delete(id);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("status", "ok");
        return resp;
    }
}
