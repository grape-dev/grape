package com.grape.admin.service;

import com.grape.admin.util.RepoPaths;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 项目模板（configs/project-templates.json）列表与使用统计。
 * "使用人数" = 通过模板一键创建并托管的仓库（managed_repos）按仓库名匹配模板
 * id/default_repo_name（含前后缀变体），去重仓库所属者统计。
 */
@Service
public class TemplatesService {

    private final Path repoRoot;
    private final String templatesConfig;
    private final JdbcTemplate jdbcTemplate;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public TemplatesService(@Value("${app.skills-dir:../../skills}") String skillsDir,
                            @Value("${app.templates-config:configs/project-templates.json}") String templatesConfig,
                            @Qualifier("grapeJdbcTemplate") JdbcTemplate jdbcTemplate) {
        this.repoRoot = RepoPaths.resolveRepoRoot(skillsDir);
        this.templatesConfig = templatesConfig;
        this.jdbcTemplate = jdbcTemplate;
    }

    public Map<String, Object> listTemplates() {
        Map<String, Object> config = loadConfig();
        List<Map<String, Object>> categories = new ArrayList<>();
        List<Map<String, Object>> flat = new ArrayList<>();

        // 托管仓库：repo_name → owner 集合（用于统计模板使用）
        Map<String, Set<String>> repoOwnerByName = loadManagedReposByName();
        // 匹配统计缓存：模板 id → 使用仓库列表
        Map<String, List<Map<String, String>>> usageCache = new LinkedHashMap<>();

        Object cats = config.get("categories");
        if (cats instanceof List) {
            for (Object c : (List<?>) cats) {
                if (!(c instanceof Map)) {
                    continue;
                }
                Map<?, ?> cat = (Map<?, ?>) c;
                Map<String, Object> catOut = new LinkedHashMap<>();
                catOut.put("sceneId", cat.get("scene_id"));
                catOut.put("name", cat.get("name"));
                List<Map<String, Object>> templates = new ArrayList<>();
                Object tpls = cat.get("templates");
                if (tpls instanceof List) {
                    for (Object t : (List<?>) tpls) {
                        if (!(t instanceof Map)) {
                            continue;
                        }
                        Map<?, ?> tpl = (Map<?, ?>) t;
                        String id = str(tpl.get("id"));
                        String defaultRepoName = str(tpl.get("default_repo_name"));
                        List<Map<String, String>> usage = usageCache.computeIfAbsent(id,
                                k -> matchUsage(id, defaultRepoName, repoOwnerByName));
                        Map<String, Object> tOut = new LinkedHashMap<>();
                        tOut.put("id", id);
                        tOut.put("name", str(tpl.get("name")));
                        tOut.put("description", str(tpl.get("description")));
                        tOut.put("defaultRepoName", str(tpl.get("default_repo_name")));
                        tOut.put("usageCount", usage.size());
                        tOut.put("usageUsers", distinctOwners(usage).size());
                        tOut.put("usageRepos", usage);
                        templates.add(tOut);
                        flat.add(tOut);
                    }
                }
                catOut.put("templates", templates);
                categories.add(catOut);
            }
        }

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("categories", categories);
        result.put("total", flat.size());
        result.put("totalUsage", flat.stream().mapToInt(t -> ((Number) t.get("usageCount")).intValue()).sum());
        return result;
    }

    private Map<String, Set<String>> loadManagedReposByName() {
        Map<String, Set<String>> map = new LinkedHashMap<>();
        try {
            jdbcTemplate.queryForList(
                    "SELECT repo_name, repo_owner FROM managed_repos WHERE repo_name IS NOT NULL AND repo_name <> ''")
                    .forEach(row -> map.computeIfAbsent(String.valueOf(row.get("repo_name")),
                            k -> new LinkedHashSet<>()).add(String.valueOf(row.get("repo_owner"))));
        } catch (Exception ignored) {
            // 表不可用时返回空
        }
        return map;
    }

    /**
     * 按仓库名匹配模板：候选为模板 id 与 default_repo_name（忽略大小写与连字符/下划线），
     * 支持前后缀变体（PracticeHub-grape 命中 practice-hub）。
     */
    private List<Map<String, String>> matchUsage(String templateId, String defaultRepoName,
                                                 Map<String, Set<String>> repoOwnerByName) {
        List<Map<String, String>> usage = new ArrayList<>();
        Set<String> candidates = new LinkedHashSet<>();
        if (templateId != null && !templateId.isBlank()) {
            candidates.add(templateId);
        }
        if (defaultRepoName != null && !defaultRepoName.isBlank() && !defaultRepoName.equals(templateId)) {
            candidates.add(defaultRepoName);
        }
        if (candidates.isEmpty()) {
            return usage;
        }
        for (Map.Entry<String, Set<String>> e : repoOwnerByName.entrySet()) {
            String normRepo = normalize(e.getKey());
            boolean hit = false;
            for (String c : candidates) {
                String normCand = normalize(c);
                if (normCand.isEmpty()) {
                    continue;
                }
                if (normRepo.equals(normCand) || normRepo.contains(normCand) || normCand.contains(normRepo)) {
                    if (normRepo.length() >= normCand.length() - 1) {
                        hit = true;
                        break;
                    }
                }
            }
            if (hit) {
                for (String owner : e.getValue()) {
                    Map<String, String> repo = new LinkedHashMap<>();
                    repo.put("repoName", e.getKey());
                    repo.put("owner", owner);
                    usage.add(repo);
                }
            }
        }
        return usage;
    }

    private String normalize(String s) {
        return s.toLowerCase().replaceAll("[-_]", "");
    }

    private Set<String> distinctOwners(List<Map<String, String>> usage) {
        Set<String> owners = new LinkedHashSet<>();
        for (Map<String, String> u : usage) {
            owners.add(u.get("owner"));
        }
        return owners;
    }

    private Map<String, Object> loadConfig() {
        // 优先用 templates-config 配置项（可为绝对路径或相对路径）
        Path cfg = Paths.get(templatesConfig).toAbsolutePath().normalize();
        if (!Files.exists(cfg)) {
            // 兜底：从 repoRoot/configs/project-templates.json 查找
            cfg = repoRoot.resolve("configs").resolve("project-templates.json");
        }
        try {
            String json = new String(Files.readAllBytes(cfg), StandardCharsets.UTF_8);
            return objectMapper.readValue(json, Map.class);
        } catch (Exception e) {
            Map<String, Object> empty = new LinkedHashMap<>();
            empty.put("categories", new ArrayList<>());
            return empty;
        }
    }

    private String str(Object o) {
        return o == null ? "" : String.valueOf(o);
    }
}
