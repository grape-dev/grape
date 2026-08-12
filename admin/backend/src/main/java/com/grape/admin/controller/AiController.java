package com.grape.admin.controller;

import com.grape.admin.config.AuthContext;
import com.grape.admin.service.AiHistoryService;
import com.grape.admin.service.AiTaskManager;
import com.grape.admin.service.AiTemplateService;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import javax.servlet.http.HttpServletRequest;
import java.util.LinkedHashMap;
import java.util.Map;

@RestController
@RequestMapping("/api/admin/ai")
public class AiController {

    private final AiTaskManager taskManager;
    private final AiHistoryService historyService;
    private final AiTemplateService templateService;

    public AiController(AiTaskManager taskManager, AiHistoryService historyService, AiTemplateService templateService) {
        this.taskManager = taskManager;
        this.historyService = historyService;
        this.templateService = templateService;
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

    /** 查询任务状态（含累积输出文本，支持同步轮询实时显示部分结果） */
    @GetMapping("/chat/task/{taskId}/status")
    public Map<String, Object> status(@PathVariable String taskId) {
        AiTaskManager.AiTask task = taskManager.get(taskId);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("taskId", task.taskId);
        resp.put("status", task.status);
        resp.put("error", task.error);
        resp.put("result", taskManager.resultText(task));
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

    // ===== 会话管理（对齐 VOD 会话列表）=====

    /** 会话列表（按 session_id 分组） */
    @GetMapping("/history/sessions")
    public Map<String, Object> sessions() {
        return historyService.sessions();
    }

    /** 某个会话的全部问答 */
    @GetMapping("/history/session/{sessionId}/messages")
    public java.util.List<Map<String, Object>> sessionMessages(@PathVariable String sessionId) {
        return historyService.sessionMessages(sessionId);
    }

    /** 会话重命名 */
    @PutMapping("/history/session/{sessionId}/name")
    public Map<String, Object> renameSession(@PathVariable String sessionId, @RequestBody Map<String, String> body) {
        historyService.rename(sessionId, body.get("name"));
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("status", "ok");
        return resp;
    }

    /** 删除整个会话 */
    @DeleteMapping("/history/session/{sessionId}")
    public Map<String, Object> deleteSession(@PathVariable String sessionId) {
        historyService.deleteBySession(sessionId);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("status", "ok");
        return resp;
    }

    // ===== AI 模板（参考 VOD AiQueryTemplateController）=====

    /** 模板列表（内置示例在前端，自定义模板在此） */
    @GetMapping("/templates")
    public Map<String, Object> templates(HttpServletRequest request) {
        return templateService.list(AuthContext.username(request));
    }

    /** 保存自定义模板 */
    @PostMapping("/templates")
    public Map<String, Object> saveTemplate(@RequestBody Map<String, String> body, HttpServletRequest request) {
        return templateService.save(AuthContext.username(request), body.get("name"), body.get("content"));
    }

    /** 删除自定义模板 */
    @DeleteMapping("/templates/{id}")
    public Map<String, Object> deleteTemplate(@PathVariable Long id, HttpServletRequest request) {
        templateService.delete(AuthContext.username(request), id);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("status", "ok");
        return resp;
    }
}
