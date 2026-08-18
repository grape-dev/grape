package com.grape.admin.service;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.grape.admin.common.BizException;
import com.grape.admin.common.NotFoundException;
import com.grape.admin.entity.AdminAiHistory;
import com.grape.admin.repository.AdminAiHistoryRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import javax.annotation.PostConstruct;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

/**
 * AI 助手异步任务管理器（参考 VOD AiQueryTaskManager）
 * submit 立即返回 taskId；后台线程执行 hermes 查询，事件缓冲在内存；
 * 前端可轮询状态或订阅流式输出；任务完成自动写入问答历史。
 */
@Service
public class AiTaskManager {

    private static final Logger log = LoggerFactory.getLogger(AiTaskManager.class);
    private static final ObjectMapper JSON_MAPPER = new ObjectMapper();
    private static final int MAX_BUFFER_LINES = 5000;

    /** 单任务状态（字段 public 供控制器读取，同 VOD AiQueryTask 风格） */
    public static class AiTask {
        public final String taskId;
        public final String query;
        public final String sessionId;
        public volatile String status = "running";   // running / done / error
        public volatile String error;
        public volatile String hermesSessionId;
        public final List<String> events = new ArrayList<>();
        public final CopyOnWriteArrayList<SseEmitter> subscribers = new CopyOnWriteArrayList<>();
        public final long createdAt = System.currentTimeMillis();
        public volatile long finishedAt;
        public volatile Process process;

        AiTask(String query, String sessionId) {
            this(query, sessionId, null);
        }

        AiTask(String query, String sessionId, String taskId) {
            this.taskId = (taskId != null && !taskId.isBlank()) ? taskId
                    : UUID.randomUUID().toString().replace("-", "").substring(0, 16);
            this.query = query;
            this.sessionId = sessionId;
        }
    }

    private final ConcurrentHashMap<String, AiTask> tasks = new ConcurrentHashMap<>();
    private final Semaphore slots = new Semaphore(3);
    private final AdminAiHistoryRepository historyRepository;
    private final AiChatService chatService;
    private ThreadPoolTaskScheduler scheduler;

    @Value("${app.hermes-bin:}")
    private String hermesBin;
    @Value("${app.hermes-query-script:ai_hermes_query.py}")
    private String queryScript;
    @Value("${app.ai-timeout-seconds:120}")
    private long timeoutSeconds;
    // 双数据源（admin-db-split v1.0）：AI 助手查 grape 业务数据（grape_test_db），
    // 由 GRAPE_DB_* 注入；旧 spring.datasource.* 键已随单数据源配置移除
    @Value("${GRAPE_DB_URL:}")
    private String dbUrl;
    @Value("${GRAPE_DB_USER:}")
    private String dbUser;
    @Value("${GRAPE_DB_PASSWORD:}")
    private String dbPassword;

    public AiTaskManager(AdminAiHistoryRepository historyRepository, AiChatService chatService) {
        this.historyRepository = historyRepository;
        this.chatService = chatService;
    }

    @PostConstruct
    void init() {
        scheduler = new ThreadPoolTaskScheduler();
        scheduler.setPoolSize(1);
        scheduler.setThreadNamePrefix("ai-timeout-");
        scheduler.initialize();
        // N6：定时清理完成任务（30 分钟一轮）——tasks Map 不无限增长
        scheduler.scheduleAtFixedRate(this::cleanupStaleTasks,
                Instant.now().plusSeconds(30 * 60), Duration.ofMinutes(30));
    }

    /** N6：清理 30 分钟前已结束的任务（运行中任务保留；历史在 DB——不受影响） */
    void cleanupStaleTasks() {
        long cutoff = System.currentTimeMillis() - 30 * 60 * 1000L;
        tasks.entrySet().removeIf(e -> {
            AiTask t = e.getValue();
            return !"running".equals(t.status) && t.finishedAt > 0 && t.finishedAt < cutoff;
        });
    }

    /** 提交任务，立即返回 taskId；并发超限抛 400 */
    public String submit(String query, String sessionId) {
        return submit(query, sessionId, null);
    }

    /** 提交任务（重试可复用 taskId——UPDATE 覆盖失败消息——一轮一条 assistant） */
    public String submit(String query, String sessionId, String taskId) {
        // 同会话运行中校验（权威禁发——防同会话并发提交——hermes --continue 同名并发）
        boolean sessionBusy = tasks.values().stream()
                .anyMatch(t -> sessionId != null && sessionId.equals(t.sessionId)
                        && "running".equals(t.status));
        if (sessionBusy) {
            throw new BizException("该会话正在处理中，请等待完成后再提问");
        }
        if (!slots.tryAcquire()) {
            throw new BizException("AI 助手并发超限（最多 3 个同时执行），请稍后重试");
        }
        AiTask task = new AiTask(query, sessionId, taskId);
        tasks.put(task.taskId, task);
        Thread t = new Thread(() -> run(task), "ai-task-" + task.taskId);
        t.setDaemon(true);
        t.start();
        // 超时兜底：杀掉进程，任务标记 error
        scheduler.schedule(() -> {
            if ("running".equals(task.status) && task.process != null && task.process.isAlive()) {
                log.warn("AI 任务 {} 超时({}s)，kill 子进程", task.taskId, timeoutSeconds);
                task.process.destroyForcibly();
            }
        }, Instant.now().plusSeconds(timeoutSeconds));
        return task.taskId;
    }

    public AiTask get(String taskId) {
        AiTask task = tasks.get(taskId);
        if (task == null) throw new NotFoundException("AI 任务不存在或已过期");
        return task;
    }

    /** 查询任务（不存在返回 null——不抛 404——status 接口查库兜底用） */
    public AiTask getOrNull(String taskId) {
        return tasks.get(taskId);
    }

    /** 任务累积输出文本（token 事件拼接），供轮询模式实时显示部分结果 */
    public String resultText(AiTask task) {
        StringBuilder sb = new StringBuilder();
        synchronized (task) {
            for (String line : task.events) {
                JsonNode node = parseJson(line);
                if (node != null && "token".equals(node.path("type").asText())) {
                    sb.append(node.path("data").asText(""));
                }
            }
        }
        return sb.toString();
    }

    /** 订阅任务输出：先重放缓冲，再实时追加；终态后自动 complete */
    public void subscribe(String taskId, SseEmitter emitter) {
        AiTask task = tasks.get(taskId);
        if (task == null) {
            emitter.completeWithError(new NotFoundException("AI 任务不存在或已过期"));
            return;
        }
        synchronized (task) {
            for (String line : task.events) {
                if (!send(emitter, line)) return;
            }
            if (!"running".equals(task.status)) {
                completeTaskEmitter(emitter, task);
                return;
            }
            task.subscribers.add(emitter);
        }
    }

    // ---------- 内部 ----------

    private void run(AiTask task) {
        Process p = null;
        try {
            p = spawn(task.query, task.sessionId);
            task.process = p;
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(p.getInputStream(), StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    if (line.isBlank()) continue;
                    synchronized (task) {
                        if (task.events.size() < MAX_BUFFER_LINES) task.events.add(line);
                        flush(task);
                    }
                    if ("done".equals(task.status)) break;
                }
            }
            int code = p.waitFor();
            if ("running".equals(task.status)) {
                task.status = code == 0 ? "done" : "error";
                if (code != 0) task.error = "hermes 进程退出码 " + code;
            }
        } catch (IOException e) {
            task.status = "error";
            task.error = "启动 hermes 失败: " + e.getMessage();
            log.error("AI 任务 {} 启动失败", task.taskId, e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            task.status = "error";
            task.error = "任务被中断";
        } finally {
            task.finishedAt = System.currentTimeMillis();
            synchronized (task) {
                saveHistory(task);   // 终态统一写消息（成功写答案——失败写失败提示——task_id 幂等）
                flush(task);
                for (SseEmitter e : task.subscribers) completeTaskEmitter(e, task);
                task.subscribers.clear();
            }
            slots.release();
        }
    }

    private void flush(AiTask task) {
        if (task.events.isEmpty()) return;
        String last = task.events.get(task.events.size() - 1);
        for (SseEmitter e : task.subscribers) {
            if (!send(e, last)) task.subscribers.remove(e);
        }
    }

    /** 任务完成写消息（新表 admin_ai_chat_message——task_id 幂等——成功写答案/失败写失败提示） */
    private void saveHistory(AiTask task) {
        try {
            StringBuilder answer = new StringBuilder();
            for (String line : task.events) {
                JsonNode node = parseJson(line);
                if (node != null && "token".equals(node.path("type").asText())) {
                    answer.append(node.path("data").asText(""));
                }
            }
            if (task.sessionId == null) return;
            String content = answer.toString();
            boolean failed = !"done".equals(task.status);
            String errorCode = null, errorMsg = null;
            if (failed) {
                content = "（查询失败，可重试）";
                // 提取错误信息（error 事件——后台排查）
                for (String line : task.events) {
                    JsonNode node = parseJson(line);
                    if (node != null && "error".equals(node.path("type").asText())) {
                        String data = node.path("data").asText("");
                        if (!data.isEmpty()) { errorMsg = data.length() > 500 ? data.substring(0, 500) : data; break; }
                    }
                }
                if (errorMsg == null) errorMsg = "任务执行失败（超时或异常）";
                errorCode = "AI_GENERATE_FAILED";
            } else if (content.isEmpty()) {
                content = "（无返回内容）";
            }
            chatService.saveAssistantAnswer(task.sessionId, task.taskId, content, failed, errorCode, errorMsg);
        } catch (Exception e) {
            log.warn("AI 消息保存失败 task={}", task.taskId, e);
        }
    }

    private JsonNode parseJson(String line) {
        try {
            return JSON_MAPPER.readTree(line);
        } catch (Exception e) {
            return null;
        }
    }

    private boolean send(SseEmitter emitter, String line) {
        try {
            emitter.send(SseEmitter.event().data(line));
            return true;
        } catch (Exception e) {
            try { emitter.complete(); } catch (Exception ignored) { }
            return false;
        }
    }

    private void completeTaskEmitter(SseEmitter emitter, AiTask task) {
        try {
            if ("error".equals(task.status)) {
                emitter.send(SseEmitter.event().data(
                        "{\"type\":\"error\",\"data\":\"" + safe(task.error) + "\"}"));
            }
            emitter.complete();
        } catch (Exception ignored) { }
    }

    private String safe(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    private Process spawn(String query, String sessionId) throws IOException {
        String scriptPath = resolveScript();
        List<String> cmd = new ArrayList<>();
        cmd.add("python3");
        cmd.add(scriptPath);
        cmd.add("--query");
        cmd.add(query);
        if (sessionId != null && !sessionId.isBlank()) {
            cmd.add("--session-id");
            cmd.add(sessionId);
        }
        ProcessBuilder pb = new ProcessBuilder(cmd);
        pb.environment().putIfAbsent("HERMES_BIN", hermesBin);
        applyDbEnv(pb.environment());
        return pb.start();
    }

    private String resolveScript() throws IOException {
        String name = queryScript == null || queryScript.isBlank() ? "ai_hermes_query.py" : queryScript;
        Path p = Path.of(name).toAbsolutePath();
        if (!Files.exists(p)) {
            p = Path.of(System.getProperty("user.dir"), name);
        }
        if (!Files.exists(p)) {
            throw new IOException("AI 查询脚本不存在: " + p);
        }
        return p.toString();
    }

    private void applyDbEnv(Map<String, String> env) {
        if (dbUrl == null || dbUrl.isBlank()) return;
        // jdbc:mysql://host:port/dbname?...
        String rest = dbUrl.replaceFirst("^jdbc:mysql://", "");
        String[] parts = rest.split("[?/]");
        if (parts.length >= 2) {
            String[] hp = parts[0].split(":");
            env.put("GRAPE_DB_HOST", hp[0]);
            if (hp.length > 1) env.put("GRAPE_DB_PORT", hp[1]);
            env.put("GRAPE_DB_NAME", parts[1]);
            env.put("GRAPE_DB_USER", dbUser == null ? "" : dbUser);
            env.put("GRAPE_DB_PASSWORD", dbPassword == null ? "" : dbPassword);
        }
    }
}
