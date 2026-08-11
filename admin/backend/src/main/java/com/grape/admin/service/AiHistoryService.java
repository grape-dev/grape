package com.grape.admin.service;

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
}
