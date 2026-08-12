package com.grape.admin.service;

import com.grape.admin.entity.readonly.ManagedRepoRo;
import com.grape.admin.repository.readonly.ManagedRepoRoRepository;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

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

    public RepoService(ManagedRepoRoRepository repoRepository, JdbcTemplate jdbcTemplate) {
        this.repoRepository = repoRepository;
        this.jdbcTemplate = jdbcTemplate;
    }

    public Map<String, Object> listRepos(String keyword, int page, int size) {
        int safeSize = size <= 0 ? 10 : size;
        int safePage = Math.max(page, 0);
        List<ManagedRepoRo> all = repoRepository.findAll().stream()
                .filter(r -> keyword == null || keyword.isBlank()
                        || containsIgnoreCase(r.getRepoKey(), keyword)
                        || containsIgnoreCase(r.getFullName(), keyword)
                        || containsIgnoreCase(r.getRepoOwner(), keyword)
                        || containsIgnoreCase(r.getRepoName(), keyword))
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
