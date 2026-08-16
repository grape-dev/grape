package com.grape.admin.controller;

import com.grape.admin.config.AuthContext;
import com.grape.admin.entity.AdminAiChatMessage;
import com.grape.admin.entity.AdminAiChatSession;
import com.grape.admin.service.AiChatService;
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
    private final AiChatService chatService;

    public AiController(AiTaskManager taskManager, AiHistoryService historyService, AiTemplateService templateService, AiChatService chatService) {
        this.taskManager = taskManager;
        this.historyService = historyService;
        this.templateService = templateService;
        this.chatService = chatService;
    }

    /** 兼容直连模式：提交 + 订阅一步完成（老客户端/单请求场景） */
    @PostMapping(value = "/chat", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter chat(@RequestBody Map<String, String> body) {
        String taskId = taskManager.submit(body.getOrDefault("query", ""), body.get("sessionId"));
        SseEmitter emitter = new SseEmitter(300_000L);
        taskManager.subscribe(taskId, emitter);
        return emitter;
    }

    /** 异步提交：立即返回 taskId + sessionId（新表会话——无 sessionId 则建会话——归属校验） */
    @PostMapping("/chat/submit")
    public Map<String, Object> submit(@RequestBody Map<String, String> body, HttpServletRequest request) {
        String query = body.getOrDefault("query", "");
        String sessionId = body.get("sessionId");
        String username = AuthContext.username(request);
        // 会话处理（新表）：无 sessionId → 建会话（UUID——绑定 hermes）；有 → 归属校验
        if (sessionId == null || sessionId.isBlank()) {
            sessionId = chatService.createSession(username, query).getSessionId();
        } else {
            chatService.checkOwnership(sessionId, username);
        }
        // 提交任务（脚本 --continue <会话 UUID>——前端不感知 hermes 内部 ID）
        String taskId = taskManager.submit(query, sessionId, body.get("taskId"));
        // 写 user 消息（task_id = taskId——幂等配对）——重试（taskId 复用）不重复写（原 user 已存在）
        String retryTaskId = body.get("taskId");
        if (retryTaskId == null || retryTaskId.isBlank()) {
            chatService.addMessage(sessionId, taskId, "user", query);
        }
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("taskId", taskId);
        resp.put("sessionId", sessionId);
        resp.put("status", "running");
        return resp;
    }

    /** 会话列表（当前用户——updated_at 降序——limit/offset 分页） */
    @GetMapping("/chat/sessions")
    public Map<String, Object> sessions(@RequestParam(defaultValue = "20") int limit,
                                        @RequestParam(required = false) String cursorTime,
                                        @RequestParam(required = false) String cursorId,
                                        @RequestParam(defaultValue = "0") int offset,
                                        HttpServletRequest request) {
        String username = AuthContext.username(request);
        boolean cursorMode = cursorTime != null && !cursorTime.isBlank();
        java.util.List<AdminAiChatSession> list = chatService.listSessions(username, limit, cursorTime, cursorId, offset);
        Map<String, Object> resp = new LinkedHashMap<>();
        if (cursorMode) {
            // 游标模式（滚动加载）：limit+1 判断 hasMore
            boolean hasMore = list.size() > limit;
            if (hasMore) list = list.subList(0, limit);
            resp.put("sessions", list);
            resp.put("hasMore", hasMore);
        } else {
            // offset 模式（页码分页——兼容后期正常分页查询）
            long total = chatService.countSessions(username);
            resp.put("sessions", list);
            resp.put("hasMore", (offset + list.size()) < total);
            resp.put("total", total);
        }
        return resp;
    }

    /** 会话消息（游标分页——首次 cursorSeq 不传取最新 40——滚动传 minSeq 取更早 20） */
    @GetMapping("/chat/session/{id}/messages")
    public Map<String, Object> messages(@PathVariable String id,
                                        @RequestParam(required = false) Long cursorSeq,
                                        @RequestParam(defaultValue = "40") int limit,
                                        HttpServletRequest request) {
        AiChatService.PageResult page = chatService.listMessages(id, AuthContext.username(request), cursorSeq, limit);
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("sessionId", id);
        resp.put("messages", page.messages);
        resp.put("hasMore", page.hasMore);
        resp.put("minSeq", page.minSeq);
        return resp;
    }

    /** 会话重命名（归属校验） */
    @PutMapping("/chat/session/{id}/name")
    public Map<String, Object> rename(@PathVariable String id, @RequestBody Map<String, String> body, HttpServletRequest request) {
        chatService.renameSession(id, AuthContext.username(request), body.getOrDefault("name", "新对话"));
        return Map.of("code", 0);
    }

    /** 删除会话（软删会话 + 消息——归属校验） */
    @DeleteMapping("/chat/session/{id}")
    public Map<String, Object> deleteSession(@PathVariable String id, HttpServletRequest request) {
        chatService.deleteSession(id, AuthContext.username(request));
        return Map.of("code", 0);
    }

    /** 查询任务状态（含累积输出文本，支持同步轮询实时显示部分结果）
     *  内存 Map 优先（实时）——内存无（清理/重启）→ 查库兜底（任务状态
     *  持久化——done/error——不再 404；无任何记录 → 200 not_found） */
    @GetMapping("/chat/task/{taskId}/status")
    public Map<String, Object> status(@PathVariable String taskId) {
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("taskId", taskId);
        AiTaskManager.AiTask task = taskManager.getOrNull(taskId);
        if (task != null) {
            resp.put("status", task.status);
            resp.put("error", task.error);
            resp.put("result", taskManager.resultText(task));
            resp.put("sessionId", task.hermesSessionId);
            resp.put("elapsedMs", System.currentTimeMillis() - task.createdAt);
            return resp;
        }
        String dbStatus = chatService.getTaskFinalStatus(taskId);
        if (dbStatus != null) {
            resp.put("status", dbStatus);   // done/error（持久化状态——不 404）
            return resp;
        }
        resp.put("status", "not_found");    // 无任何记录——200（消除 HTTP 404 噪音）
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
