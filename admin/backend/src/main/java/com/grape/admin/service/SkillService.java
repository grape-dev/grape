package com.grape.admin.service;

import com.grape.admin.common.NotFoundException;
import com.grape.admin.entity.readonly.SkillRo;
import com.grape.admin.repository.readonly.SkillRoRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

/**
 * Skill 技能库读服务：直读共享库 skill 表（官方/仓库/个人三态技能），支持关键字/Agent/范围筛选 + 分页，
 * 以及读取技能目录下的 SKILL.md 完整内容。
 */
@Service
public class SkillService {

    /** skill_path 白名单：仅允许字母数字/下划线/中划线/点（防路径穿越，只读技能目录内文件） */
    private static final Pattern SAFE_SKILL_PATH = Pattern.compile("^[\\w\\-.]+$");

    private final SkillRoRepository skillRepository;
    private final Path skillsDir;

    public SkillService(SkillRoRepository skillRepository,
                        @Value("${app.skills-dir:../../skills}") String skillsDir) {
        this.skillRepository = skillRepository;
        this.skillsDir = Paths.get(skillsDir).toAbsolutePath().normalize();
    }

    public Map<String, Object> listSkills(String keyword, String agentType, String scope, int page, int size) {
        List<SkillRo> all = skillRepository.findAll().stream()
                .filter(s -> keyword == null || keyword.isBlank()
                        || containsIgnoreCase(s.getSkillName(), keyword)
                        || containsIgnoreCase(s.getDisplayName(), keyword)
                        || containsIgnoreCase(s.getDescription(), keyword))
                .filter(s -> agentType == null || agentType.isBlank() || agentType.equalsIgnoreCase(s.getAgentType()))
                .filter(s -> scope == null || scope.isBlank() || scope.equalsIgnoreCase(s.getScope()))
                .sorted(Comparator.comparing(SkillRo::getSkillName, String.CASE_INSENSITIVE_ORDER))
                .collect(Collectors.toList());

        int totalElements = all.size();
        int safeSize = size <= 0 ? 10 : size;
        int totalPages = (int) Math.ceil((double) totalElements / safeSize);
        int from = Math.min(page * safeSize, totalElements);
        int to = Math.min(from + safeSize, totalElements);
        List<Map<String, Object>> content = all.subList(from, to).stream().map(this::toMap)
                .collect(Collectors.toList());

        Map<String, Long> stats = all.stream()
                .collect(Collectors.groupingBy(s -> s.getScope() == null ? "" : s.getScope(), Collectors.counting()));
        // Agent 类型选项基于全量数据统计（不随当前筛选收窄，避免下拉选项越选越少）
        Map<String, Long> agentStats = skillRepository.findAll().stream()
                .collect(Collectors.groupingBy(s -> s.getAgentType() == null ? "" : s.getAgentType(), Collectors.counting()));

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("content", content);
        result.put("totalElements", totalElements);
        result.put("totalPages", totalPages);
        result.put("stats", stats);
        result.put("agentStats", agentStats);
        return result;
    }

    public Map<String, Object> getSkillContent(Integer id) {
        SkillRo skill = skillRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("技能不存在: id=" + id));
        String skillPath = skill.getSkillPath();
        if (skillPath == null || skillPath.isBlank() || !SAFE_SKILL_PATH.matcher(skillPath).matches()) {
            throw new NotFoundException("技能路径不合法: " + skillPath);
        }
        Path md = skillsDir.resolve(skillPath).resolve("SKILL.md").normalize();
        if (!md.startsWith(skillsDir) || !Files.isRegularFile(md)) {
            throw new NotFoundException("未找到 SKILL.md: " + skillPath);
        }
        String content;
        try {
            content = new String(Files.readAllBytes(md), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new NotFoundException("读取 SKILL.md 失败: " + skillPath);
        }
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("id", skill.getId());
        result.put("skillName", skill.getSkillName());
        result.put("displayName", skill.getDisplayName());
        result.put("scope", skill.getScope());
        result.put("content", content);
        return result;
    }

    private Map<String, Object> toMap(SkillRo s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", s.getId());
        m.put("skillName", s.getSkillName());
        m.put("displayName", s.getDisplayName());
        m.put("description", s.getDescription());
        m.put("agentType", s.getAgentType());
        m.put("skillPath", s.getSkillPath());
        m.put("scope", s.getScope());
        m.put("ownerId", s.getOwnerId());
        m.put("isActive", s.getIsActive());
        m.put("createdAt", s.getCreatedAt());
        m.put("updatedAt", s.getUpdatedAt());
        return m;
    }

    private static boolean containsIgnoreCase(String haystack, String needle) {
        return haystack != null && haystack.toLowerCase().contains(needle.toLowerCase());
    }
}
