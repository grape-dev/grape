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
            this.taskId = UUID.randomUUID().toString().replace("-", "").substring(0, 16);
            this.query = query;
            this.sessionId = sessionId;
        }
    }

    private final ConcurrentHashMap<String, AiTask> tasks = new ConcurrentHashMap<>();
    private final Semaphore slots = new Semaphore(3);
    private final AdminAiHistoryRepository historyRepository;
    private ThreadPoolTaskScheduler scheduler;

    @Value("${app.hermes-bin:}")
    private String hermesBin;
    @Value("${app.hermes-query-script:ai_hermes_query.py}")
    private String queryScript;
    @Value("${app.ai-timeout-seconds:120}")
    private long timeoutSeconds;
    @Value("${spring.datasource.url:}")
    private String dbUrl;
    @Value("${spring.datasource.username:}")
    private String dbUser;
    @Value("${spring.datasource.password:}")
    private String dbPassword;

    public AiTaskManager(AdminAiHistoryRepository historyRepository) {
        this.historyRepository = historyRepository;
    }

    @PostConstruct
    void init() {
        scheduler = new ThreadPoolTaskScheduler();
        scheduler.setPoolSize(1);
        scheduler.setThreadNamePrefix("ai-timeout-");
        scheduler.initialize();
    }

    /** 提交任务，立即返回 taskId；并发超限抛 400 */
    public String submit(String query, String sessionId) {
        if (!slots.tryAcquire()) {
            throw new BizException("AI 助手并发超限（最多 3 个同时执行），请稍后重试");
        }
        AiTask task = new AiTask(query, sessionId);
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
                extractSessionId(task);
                if ("done".equals(task.status)) saveHistory(task);
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

    /** 从事件流中提取 hermes 会话 id（session 事件） */
    private void extractSessionId(AiTask task) {
        for (String line : task.events) {
            JsonNode node = parseJson(line);
            if (node != null && "session".equals(node.path("type").asText())) {
                task.hermesSessionId = node.path("data").asText(null);
                break;
            }
        }
    }

    /** 任务完成自动写历史（taskId 唯一幂等） */
    private void saveHistory(AiTask task) {
        try {
            StringBuilder answer = new StringBuilder();
            for (String line : task.events) {
                JsonNode node = parseJson(line);
                if (node != null && "token".equals(node.path("type").asText())) {
                    answer.append(node.path("data").asText(""));
                }
            }
            if (task.hermesSessionId == null || answer.length() == 0) return;
            AdminAiHistory h = new AdminAiHistory();
            h.setSessionId(task.hermesSessionId);
            h.setTaskId(task.taskId);
            h.setQuery(task.query);
            h.setName(task.query.length() > 50 ? task.query.substring(0, 50) : task.query);
            h.setAnswer(answer.toString());
            historyRepository.save(h);
        } catch (Exception e) {
            log.warn("AI 历史保存失败 task={}", task.taskId, e);
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
