package com.grape.admin.service;

import com.grape.admin.common.BizException;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;
import org.springframework.stereotype.Service;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import javax.annotation.PostConstruct;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * AI 助手：spawn ai_hermes_query.py（移植 VOD AiQueryService 模式）→ 流式 JSON 事件 → SSE 转发。
 * 并发上限 3（超出发 error 事件）；单次流式超时 120s（到时 kill 子进程）。
 */
@Service
public class AiAssistantService {

    private static final Logger log = LoggerFactory.getLogger(AiAssistantService.class);
    private static final Pattern JDBC_URL = Pattern.compile("jdbc:mysql://([^:/]+):(\\d+)/([^?]+)");

    private final Semaphore slots = new Semaphore(3);
    private final ThreadPoolTaskScheduler scheduler = new ThreadPoolTaskScheduler();

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

    @PostConstruct
    void init() {
        scheduler.setThreadNamePrefix("ai-assistant-");
        scheduler.initialize();
    }

    public void stream(String query, String sessionId, SseEmitter emitter) {
        if (!slots.tryAcquire()) {
            sendJson(emitter, "{\"type\":\"error\",\"data\":\"并发超限：最多同时 3 个会话，请稍后重试\"}");
            emitter.complete();
            return;
        }
        try {
            Path script = resolveScript();
            List<String> cmd = new ArrayList<>(List.of("python3", script.toString(), "--query", query));
            if (sessionId != null && !sessionId.isBlank()) {
                cmd.add("--session-id");
                cmd.add(sessionId);
            }
            ProcessBuilder pb = new ProcessBuilder(cmd);
            Map<String, String> env = pb.environment();
            if (hermesBin != null && !hermesBin.isBlank()) {
                env.put("HERMES_BIN", hermesBin);
            }
            applyDbHint(env);
            Process process = pb.start();

            scheduler.schedule(() -> {
                if (process.isAlive()) {
                    log.warn("AI 助手超时({}s)，kill 子进程", timeoutSeconds);
                    process.destroyForcibly();
                }
            }, java.time.Instant.now().plusSeconds(timeoutSeconds));

            new Thread(() -> {
                try (BufferedReader reader = new BufferedReader(
                        new InputStreamReader(process.getInputStream(), StandardCharsets.UTF_8))) {
                    String line;
                    while ((line = reader.readLine()) != null) {
                        if (!line.isBlank()) {
                            sendJson(emitter, line);
                        }
                    }
                } catch (Exception e) {
                    sendJson(emitter, "{\"type\":\"error\",\"data\":\"流式读取中断\"}");
                } finally {
                    emitter.complete();
                    slots.release();
                }
            }, "ai-assistant-reader").start();
        } catch (Exception e) {
            slots.release();
            sendJson(emitter, "{\"type\":\"error\",\"data\":\"" + escape(e.getMessage()) + "\"}");
            emitter.complete();
        }
    }

    private Path resolveScript() throws Exception {
        Path p = Path.of(queryScript);
        if (!p.isAbsolute()) {
            p = Path.of(System.getProperty("user.dir")).resolve(queryScript);
        }
        if (!Files.exists(p)) {
            throw new BizException("AI 代理脚本不存在: " + p);
        }
        return p.toAbsolutePath();
    }

    /** 把 admin 数据源信息透传给 hermes 作为只读查询提示（prod 时该账号即 D5 最小权限账号） */
    private void applyDbHint(Map<String, String> env) {
        Matcher m = JDBC_URL.matcher(dbUrl == null ? "" : dbUrl);
        if (m.find() && dbUser != null && !dbUser.isBlank()) {
            env.put("GRAPE_DB_HOST", m.group(1));
            env.put("GRAPE_DB_USER", dbUser);
            env.put("GRAPE_DB_PASSWORD", dbPassword == null ? "" : dbPassword);
            env.put("GRAPE_DB_NAME", m.group(3));
        }
    }

    private void sendJson(SseEmitter emitter, String json) {
        try {
            emitter.send(SseEmitter.event().data(json));
        } catch (Exception e) {
            // 客户端断开：忽略，读取线程会在 finally complete
        }
    }

    private String escape(String s) {
        return s == null ? "" : s.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
