package com.grape.admin.service;

import com.grape.admin.common.BizException;
import com.grape.admin.common.NotFoundException;
import com.grape.admin.entity.AdminAiHistory;
import com.grape.admin.repository.AdminAiHistoryRepository;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.stereotype.Service;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.stream.Collectors;

@Service
public class AiHistoryService {

    private final AdminAiHistoryRepository historyRepository;

    public AiHistoryService(AdminAiHistoryRepository historyRepository) {
        this.historyRepository = historyRepository;
    }

    public Map<String, Object> list(String keyword, int page, int size) {
        PageRequest pr = PageRequest.of(Math.max(page, 0), Math.min(Math.max(size, 1), 50),
                Sort.by(Sort.Direction.DESC, "id"));
        Page<AdminAiHistory> p = (keyword == null || keyword.isBlank())
                ? historyRepository.findAll(pr)
                : historyRepository.findByQueryContainingIgnoreCase(keyword.trim(), pr);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("content", p.getContent().stream().map(h -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("id", h.getId());
            m.put("sessionId", h.getSessionId());
            m.put("query", h.getQuery());
            m.put("answer", h.getAnswer());
            m.put("createdAt", h.getCreatedAt() == null ? null : h.getCreatedAt().toString().replace("T", " "));
            return m;
        }).collect(Collectors.toList()));
        result.put("totalElements", p.getTotalElements());
        result.put("totalPages", p.getTotalPages());
        result.put("page", page);
        result.put("size", size);
        return result;
    }

    public void delete(Long id) {
        AdminAiHistory h = historyRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("历史记录不存在"));
        h.setDeleted(1);
        historyRepository.save(h);
    }

    /** 会话列表（按 session_id 分组，对齐 VOD 会话列表）：name=首个问题，messageCount=消息数 */
    public Map<String, Object> sessions() {
        java.util.List<AdminAiHistory> all = historyRepository.findAll();
        Map<String, Map<String, Object>> bySession = new java.util.LinkedHashMap<>();
        for (AdminAiHistory h : all) {
            if (h.getSessionId() == null) continue;
            Map<String, Object> s = bySession.computeIfAbsent(h.getSessionId(), k -> {
                Map<String, Object> m = new java.util.LinkedHashMap<>();
                m.put("sessionId", k);
                m.put("name", h.getName() != null ? h.getName() : h.getQuery());
                m.put("messageCount", 0);
                m.put("lastAt", h.getCreatedAt() == null ? "" : h.getCreatedAt().toString().replace("T", " "));
                return m;
            });
            s.put("messageCount", (Integer) s.get("messageCount") + 1);
            String lastAt = h.getCreatedAt() == null ? "" : h.getCreatedAt().toString().replace("T", " ");
            if (lastAt.compareTo((String) s.get("lastAt")) > 0) s.put("lastAt", lastAt);
        }
        java.util.List<Map<String, Object>> list = new java.util.ArrayList<>(bySession.values());
        list.sort((a, b) -> ((String) b.get("lastAt")).compareTo((String) a.get("lastAt")));
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("sessions", list);
        result.put("count", list.size());
        return result;
    }

    /** 某个会话的全部问答（按时间正序，用于对话展示） */
    public java.util.List<Map<String, Object>> sessionMessages(String sessionId) {
        return historyRepository.findBySessionIdAndDeletedOrderByIdAsc(sessionId, 0).stream().map(h -> {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("query", h.getQuery());
            m.put("answer", h.getAnswer());
            m.put("createdAt", h.getCreatedAt() == null ? null : h.getCreatedAt().toString().replace("T", " "));
            return m;
        }).collect(Collectors.toList());
    }

    /** 会话重命名（对齐 VOD 双击重命名） */
    public void rename(String sessionId, String name) {
        if (name == null || name.trim().isEmpty()) throw new com.grape.admin.common.BizException("会话名不能为空");
        java.util.List<AdminAiHistory> rows = historyRepository.findBySessionIdAndDeletedOrderByIdAsc(sessionId, 0);
        if (rows.isEmpty()) throw new NotFoundException("会话不存在");
        String clean = name.trim();
        for (AdminAiHistory h : rows) {
            h.setName(clean);
            historyRepository.save(h);
        }
    }

    /** 删除整个会话（该 session_id 的全部问答） */
    public void deleteBySession(String sessionId) {
        java.util.List<AdminAiHistory> rows = historyRepository.findBySessionIdAndDeletedOrderByIdAsc(sessionId, 0);
        if (rows.isEmpty()) throw new NotFoundException("会话不存在");
        for (AdminAiHistory h : rows) {
            h.setDeleted(1);
            historyRepository.save(h);
        }
    }
}
