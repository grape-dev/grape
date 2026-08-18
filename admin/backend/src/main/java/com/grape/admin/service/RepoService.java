package com.grape.admin.service;

import com.grape.admin.entity.readonly.ManagedRepoRo;
import com.grape.admin.repository.readonly.ManagedRepoRoRepository;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

/**
 * 代码仓管理读服务：直读共享库 managed_repos（已托管代码仓），
 * 聚合 events 表展示活跃度（事件总数 + 最近事件时间），支持关键字过滤 + 场景统计。
 * 敏感字段（bot token / webhook token）不返回。
 */
@Service
public class RepoService {

    private final ManagedRepoRoRepository repoRepository;
    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public RepoService(ManagedRepoRoRepository repoRepository, @Qualifier("grapeJdbcTemplate") JdbcTemplate jdbcTemplate) {
        this.repoRepository = repoRepository;
        this.jdbcTemplate = jdbcTemplate;
    }

    public Map<String, Object> listRepos(String keyword, String scene, int page, int size) {
        int safeSize = size <= 0 ? 10 : size;
        int safePage = Math.max(page, 0);
        List<ManagedRepoRo> all = repoRepository.findAll().stream()
                .filter(r -> keyword == null || keyword.isBlank()
                        || containsIgnoreCase(r.getRepoKey(), keyword)
                        || containsIgnoreCase(r.getFullName(), keyword)
                        || containsIgnoreCase(r.getRepoOwner(), keyword)
                        || containsIgnoreCase(r.getRepoName(), keyword))
                .filter(r -> scene == null || scene.isBlank()
                        || (r.getDevScene() == null ? "default" : r.getDevScene()).equals(scene))
                .sorted(Comparator.comparing(ManagedRepoRo::getRepoKey, String.CASE_INSENSITIVE_ORDER))
                .collect(Collectors.toList());

        int totalElements = all.size();
        int totalPages = (int) Math.ceil((double) totalElements / safeSize);
        int from = Math.min(safePage * safeSize, totalElements);
        int to = Math.min(from + safeSize, totalElements);

        // events 表聚合：repo_key → {事件总数, 失败数, 最近事件时间}
        Map<String, Map<String, Object>> eventStats = loadEventStats();

        List<Map<String, Object>> content = all.subList(from, to).stream()
                .map(r -> toMap(r, eventStats.get(r.getRepoKey())))
                .collect(Collectors.toList());

        Map<String, Long> sceneStats = all.stream()
                .collect(Collectors.groupingBy(r -> r.getDevScene() == null || r.getDevScene().isBlank()
                        ? "default" : r.getDevScene(), Collectors.counting()));

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("content", content);
        result.put("totalElements", totalElements);
        result.put("totalPages", totalPages);
        result.put("sceneStats", sceneStats);
        return result;
    }

    /**
     * 仓库事件时序：按 issue 分组（组内按时间排序），用于 A2A 流程时序图。
     */
    public Map<String, Object> timeline(String repoKey) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("repoKey", repoKey);
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(
                "SELECT seq, event_type, issue_id, title, actor, agent_type, skill, status, duration, created_at, payload "
                        + "FROM events WHERE repo_key = ? AND issue_id IS NOT NULL AND issue_id <> '' ORDER BY created_at", repoKey);
        Map<String, List<Map<String, Object>>> group = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            String issueId = (String) row.get("issue_id");
            group.computeIfAbsent(issueId, k -> new ArrayList<>()).add(row);
        }
        List<Map<String, Object>> issues = new ArrayList<>();
        for (Map.Entry<String, List<Map<String, Object>>> e : group.entrySet()) {
            Map<String, Object> g = new LinkedHashMap<>();
            g.put("issueId", e.getKey());
            List<Map<String, Object>> evs = e.getValue();
            // 取第一个非空标题（从 payload 解析）
            String title = null;
            for (Map<String, Object> ev : evs) {
                title = issueTitleFromPayload(ev);
                if (title != null && !title.isBlank()) break;
            }
            g.put("title", title);
            g.put("eventCount", evs.size());
            g.put("firstAt", evs.get(0).get("created_at"));
            g.put("lastAt", evs.get(evs.size() - 1).get("created_at"));
            // 精简事件字段
            List<Map<String, Object>> slim = new ArrayList<>();
            for (Map<String, Object> ev : evs) {
                Map<String, Object> s = new LinkedHashMap<>();
                s.put("seq", ev.get("seq"));
                s.put("eventType", ev.get("event_type"));
                s.put("agentType", ev.get("agent_type"));
                s.put("skill", ev.get("skill"));
                s.put("status", ev.get("status"));
                s.put("duration", ev.get("duration"));
                s.put("createdAt", ev.get("created_at"));
                s.put("actor", ev.get("actor"));
                slim.add(s);
            }
            g.put("events", slim);
            issues.add(g);
        }
        result.put("issues", issues);
        return result;
    }

    /** 从 payload JSON 提取 issue 标题（GitCode webhook 格式：object_attributes.title）。 */
    private String issueTitleFromPayload(Map<String, Object> row) {
        try {
            Object payload = row.get("payload");
            if (payload == null || !(payload instanceof String) || ((String) payload).isBlank()) return null;
            Map<String, Object> attrs = objectMapper.readValue((String) payload, Map.class);
            // 尝试多种字段源
            if (attrs.containsKey("object_attributes")) {
                Map<String, Object> oa = (Map<String, Object>) attrs.get("object_attributes");
                if (oa.containsKey("title")) return (String) oa.get("title");
            }
            if (attrs.containsKey("issue")) {
                Map<String, Object> iss = (Map<String, Object>) attrs.get("issue");
                if (iss.containsKey("title")) return (String) iss.get("title");
            }
            return null;
        } catch (Exception e) {
            return null;
        }
    }

    private Map<String, Map<String, Object>> loadEventStats() {
        List<Map<String, Object>> rows = jdbcTemplate.queryForList(
                "SELECT repo_key AS repoKey, COUNT(*) AS eventCount, "
                        + "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failedCount, "
                        + "MAX(created_at) AS lastEventAt "
                        + "FROM events GROUP BY repo_key");
        Map<String, Map<String, Object>> stats = new LinkedHashMap<>();
        for (Map<String, Object> row : rows) {
            Map<String, Object> stat = new LinkedHashMap<>();
            stat.put("eventCount", row.get("eventCount"));
            stat.put("failedCount", row.get("failedCount"));
            stat.put("lastEventAt", row.get("lastEventAt"));
            stats.put(String.valueOf(row.get("repoKey")), stat);
        }
        return stats;
    }

    private Map<String, Object> toMap(ManagedRepoRo r, Map<String, Object> eventStat) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("repoKey", r.getRepoKey());
        m.put("repoOwner", r.getRepoOwner());
        m.put("repoName", r.getRepoName());
        m.put("fullName", r.getFullName());
        m.put("webhookId", r.getWebhookId());
        m.put("devScene", r.getDevScene());
        m.put("botUsername", r.getBotUsername());
        m.put("createdAt", r.getCreatedAt());
        m.put("isAdmin", r.getIsAdmin());
        if (eventStat != null) {
            m.put("eventCount", eventStat.get("eventCount"));
            m.put("failedCount", eventStat.get("failedCount"));
            m.put("lastEventAt", eventStat.get("lastEventAt"));
        } else {
            m.put("eventCount", 0);
            m.put("failedCount", 0);
            m.put("lastEventAt", null);
        }
        return m;
    }

    private static boolean containsIgnoreCase(String haystack, String needle) {
        return haystack != null && haystack.toLowerCase().contains(needle.toLowerCase());
    }
}
