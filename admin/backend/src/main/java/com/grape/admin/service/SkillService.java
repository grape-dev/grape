package com.grape.admin.service;

import com.grape.admin.common.BizException;
import com.grape.admin.common.NotFoundException;
import com.grape.admin.entity.readonly.SkillRo;
import com.grape.admin.repository.readonly.SkillRoRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.yaml.snakeyaml.Yaml;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;
import java.util.zip.ZipOutputStream;

/**
 * Skill 技能库读服务：直读共享库 skill 表（官方/仓库/个人三态技能），支持关键字/Agent/范围筛选 + 分页，
 * 读取技能目录下的 SKILL.md 完整内容，以及仓库/个人技能上传（校验规则与主项目 grape 一致）。
 */
@Service
public class SkillService {

    /** skill_path 白名单：仅允许字母数字/下划线/中划线/点（防路径穿越，只读技能目录内文件） */
    private static final Pattern SAFE_SKILL_PATH = Pattern.compile("^[\\w\\-.]+$");
    private static final Pattern SAFE_SKILL_NAME = Pattern.compile("^[a-z0-9-]+$");
    private static final Pattern SEMVER = Pattern.compile("^\\d+\\.\\d+\\.\\d+$");
    private static final Set<String> VALID_AGENT_TYPES = Set.of("vod", "issue", "coding", "qa", "sre", "chat");
    private static final Set<String> FORBIDDEN_EXTS = Set.of(".py", ".sh", ".bat", ".exe", ".jar", ".class", ".so", ".dll", ".bin");
    private static final Set<String> ALLOWED_EXTS = Set.of(".md", ".txt", ".json", ".yaml", ".yml");
    private static final Set<String> PROJECT_SKILLS = Set.of("pr", "issue", "implementation", "build-debug", "deploy-guard",
            "incident-triage", "changelog-gen", "chat", "cloud-deploy", "im-notify", "agent");
    private static final long MAX_ZIP_BYTES = 10L * 1024 * 1024;

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

    public Map<String, Object> uploadSkill(InputStream zipStream, long size, String filename,
                                           String scope, String repo, String agentType, String username) {
        // 参数校验（与主项目 grape upload 一致）
        if (scope == null || !(scope.equals("user") || scope.equals("repo"))) {
            throw new BizException("scope 必须为 user 或 repo");
        }
        if (scope.equals("repo") && (repo == null || repo.isBlank())) {
            throw new BizException("仓库技能必须指定 repo");
        }
        if (agentType == null || agentType.isBlank()) {
            throw new BizException("必须选择技能类型（agent_type）");
        }
        if (!VALID_AGENT_TYPES.contains(agentType)) {
            throw new BizException("技能类型不合法，可选: " + VALID_AGENT_TYPES.stream().sorted().collect(Collectors.joining(", ")));
        }
        if (size > MAX_ZIP_BYTES) {
            throw new BizException("Skill 文件不能超过 10MB");
        }

        Path tmpDir;
        try {
            tmpDir = Files.createTempDirectory("skill-upload-");
        } catch (IOException e) {
            throw new BizException("上传失败: " + e.getMessage());
        }
        try {
            // 解压 ZIP 到临时目录
            Path extractDir = tmpDir.resolve("extracted");
            Files.createDirectories(extractDir);
            boolean anyEntry = false;
            try (ZipInputStream zis = new ZipInputStream(zipStream)) {
                ZipEntry entry;
                while ((entry = zis.getNextEntry()) != null) {
                    anyEntry = true;
                    Path target = extractDir.resolve(entry.getName()).normalize();
                    if (!target.startsWith(extractDir)) {
                        throw new BizException("ZIP 包含非法路径: " + entry.getName());
                    }
                    if (entry.isDirectory()) {
                        Files.createDirectories(target);
                    } else {
                        Files.createDirectories(target.getParent());
                        Files.copy(zis, target, StandardCopyOption.REPLACE_EXISTING);
                    }
                }
            } catch (IOException e) {
                throw new BizException("不是有效的 ZIP 文件，请上传 .zip 格式");
            }
            if (!anyEntry) {
                throw new BizException("ZIP 文件为空");
            }

            // 过滤系统杂项目录，取第一个真实目录
            List<Path> entries;
            try (Stream<Path> s = Files.list(extractDir)) {
                entries = s.collect(Collectors.toList());
            } catch (IOException e) {
                throw new BizException("读取 ZIP 内容失败");
            }
            List<Path> realEntries = entries.stream()
                    .filter(p -> {
                        String n = p.getFileName().toString();
                        return !n.equals("__MACOSX") && !n.equals(".DS_Store") && !n.equals(".git") && !n.startsWith(".");
                    })
                    .collect(Collectors.toList());
            if (realEntries.isEmpty()) {
                throw new BizException("ZIP 中未找到技能目录");
            }
            Path skillDir = realEntries.get(0);
            if (!Files.isDirectory(skillDir)) {
                skillDir = extractDir;
            }
            String skillName = Files.isDirectory(skillDir)
                    ? skillDir.getFileName().toString()
                    : realEntries.get(0).getFileName().toString();

            // SKILL.md 校验（与主项目 validator.py 规则一致）
            List<String> errors = validateCustomSkill(skillDir, skillName);
            if (!errors.isEmpty()) {
                throw new BizException("Skill 校验失败: " + String.join("; ", errors));
            }
            Map<String, Object> fm = parseFrontmatter(skillDir);
            String description = String.valueOf(fm.getOrDefault("description", "")).trim();

            // 落盘：repo → skills/repo/<repo_key>/<skill_name>；user → skills/custom/<username>/<skill_name>
            Path targetDir;
            String ownerId;
            if (scope.equals("repo")) {
                ownerId = repo.trim();
                targetDir = skillsDir.resolve("repo").resolve(ownerId).resolve(skillName).normalize();
            } else {
                ownerId = username;
                targetDir = skillsDir.resolve("custom").resolve(ownerId).resolve(skillName).normalize();
            }
            if (!targetDir.startsWith(skillsDir)) {
                throw new BizException("技能路径非法");
            }
            Files.createDirectories(targetDir.getParent());
            if (Files.exists(targetDir)) {
                deleteRecursively(targetDir);
            }
            copyRecursively(skillDir, targetDir);

            // 入库：先删同键旧记录再插入（与主项目一致）
            skillRepository.deleteBySkillNameAndScopeAndOwnerId(skillName, scope, ownerId);
            skillRepository.flush();
            skillRepository.insertSkill(skillName, skillName, description, agentType, skillName, scope, ownerId);

            Map<String, Object> result = new LinkedHashMap<>();
            result.put("message", "技能 " + skillName + " 上传成功");
            result.put("skillName", skillName);
            return result;
        } catch (BizException e) {
            throw e;
        } catch (Exception e) {
            throw new BizException("上传失败: " + e.getMessage());
        } finally {
            deleteRecursively(tmpDir);
        }
    }

    /** 生成技能模板 ZIP（skills/_template 目录打包，与主项目一致） */
    public byte[] buildTemplateZip() {
        Path templateDir = skillsDir.resolve("_template");
        if (!Files.isDirectory(templateDir)) {
            throw new NotFoundException("技能模板未找到");
        }
        try (ByteArrayOutputStream bos = new ByteArrayOutputStream();
             ZipOutputStream zos = new ZipOutputStream(bos)) {
            try (Stream<Path> walk = Files.walk(templateDir)) {
                List<Path> files = walk.filter(Files::isRegularFile)
                        .filter(p -> !p.getFileName().toString().equals("__pycache__"))
                        .collect(Collectors.toList());
                for (Path f : files) {
                    String rel = templateDir.relativize(f).toString().replace('\\', '/');
                    zos.putNextEntry(new ZipEntry(rel));
                    Files.copy(f, zos);
                    zos.closeEntry();
                }
            }
            zos.finish();
            return bos.toByteArray();
        } catch (IOException e) {
            throw new BizException("模板打包失败: " + e.getMessage());
        }
    }

    // ====== 校验（复刻主项目 src/user/validator.py）======

    private List<String> validateCustomSkill(Path skillDir, String skillName) {
        List<String> errors = new ArrayList<>();
        Path skillPath = skillDir.resolve("SKILL.md");
        if (!Files.isRegularFile(skillPath)) {
            return List.of("缺少 SKILL.md 文件");
        }
        String content;
        try {
            content = new String(Files.readAllBytes(skillPath), StandardCharsets.UTF_8);
        } catch (IOException e) {
            return List.of("SKILL.md 读取失败");
        }
        String[] parts = content.split("---", 3);
        if (parts.length < 3) {
            errors.add("缺少 YAML Front Matter（--- 分隔）");
            return errors;
        }
        Object fmObj;
        try {
            fmObj = new Yaml().load(parts[1]);
        } catch (Exception e) {
            errors.add("Front Matter 解析失败: " + e.getMessage());
            return errors;
        }
        if (!(fmObj instanceof Map)) {
            errors.add("Front Matter 必须是一个对象");
            return errors;
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> fm = (Map<String, Object>) fmObj;
        String nameVal = String.valueOf(fm.getOrDefault("name", ""));
        if (!SAFE_SKILL_NAME.matcher(nameVal).matches()) {
            errors.add("name 字段仅允许小写字母、数字、连字符");
        }
        if (!nameVal.equals(skillName)) {
            errors.add("name（" + nameVal + "）与目录名（" + skillName + "）不一致");
        }
        String version = String.valueOf(fm.getOrDefault("version", ""));
        if (!SEMVER.matcher(version).matches()) {
            errors.add("version 字段必须符合语义化版本号格式（x.y.z）");
        }
        String desc = String.valueOf(fm.getOrDefault("description", "")).trim();
        if (desc.isEmpty()) {
            errors.add("description 字段不能为空");
        } else if (desc.length() > 200) {
            errors.add("description 字段不超过 200 字");
        }
        if (PROJECT_SKILLS.contains(nameVal)) {
            errors.add("技能名「" + nameVal + "」与项目级技能冲突，请换一个名称");
        }
        // 文件类型检查
        try (Stream<Path> walk = Files.walk(skillDir)) {
            for (Path f : walk.filter(Files::isRegularFile).collect(Collectors.toList())) {
                String name = f.getFileName().toString().toLowerCase();
                String ext = name.contains(".") ? name.substring(name.lastIndexOf('.')) : "";
                if (FORBIDDEN_EXTS.contains(ext)) {
                    errors.add("不允许上传可执行文件: " + f.getFileName());
                    return errors;
                }
                if (!ext.isEmpty() && !ALLOWED_EXTS.contains(ext) && !ext.equals(".md") && !ext.equals(".yaml")
                        && !ext.equals(".yml") && !ext.equals(".json") && !ext.equals(".txt")) {
                    errors.add("不支持的文件类型: " + f.getFileName());
                }
            }
        } catch (IOException e) {
            errors.add("技能目录读取失败");
        }
        return errors;
    }

    private Map<String, Object> parseFrontmatter(Path skillDir) {
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("name", "");
        result.put("description", "");
        result.put("version", "");
        Path skillPath = skillDir.resolve("SKILL.md");
        if (!Files.isRegularFile(skillPath)) {
            return result;
        }
        try {
            String content = new String(Files.readAllBytes(skillPath), StandardCharsets.UTF_8);
            String[] parts = content.split("---", 3);
            if (parts.length < 3) {
                return result;
            }
            Object fm = new Yaml().load(parts[1]);
            if (fm instanceof Map) {
                @SuppressWarnings("unchecked")
                Map<String, Object> m = (Map<String, Object>) fm;
                result.put("name", String.valueOf(m.getOrDefault("name", "")));
                result.put("description", String.valueOf(m.getOrDefault("description", "")).trim());
                result.put("version", String.valueOf(m.getOrDefault("version", "")));
            }
        } catch (Exception ignored) {
        }
        return result;
    }

    private void copyRecursively(Path src, Path dst) throws IOException {
        try (Stream<Path> walk = Files.walk(src)) {
            for (Path p : walk.collect(Collectors.toList())) {
                Path target = dst.resolve(src.relativize(p));
                if (Files.isDirectory(p)) {
                    Files.createDirectories(target);
                } else {
                    Files.createDirectories(target.getParent());
                    Files.copy(p, target, StandardCopyOption.REPLACE_EXISTING);
                }
            }
        }
    }

    private void deleteRecursively(Path dir) {
        if (dir == null || !Files.exists(dir)) {
            return;
        }
        try (Stream<Path> walk = Files.walk(dir)) {
            List<Path> paths = walk.sorted(Comparator.reverseOrder()).collect(Collectors.toList());
            for (Path p : paths) {
                Files.deleteIfExists(p);
            }
        } catch (IOException ignored) {
        }
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
