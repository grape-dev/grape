package com.grape.admin.service;

import com.grape.admin.common.BizException;
import com.grape.admin.common.NotFoundException;
import com.grape.admin.entity.readonly.SkillRo;
import com.grape.admin.repository.readonly.SkillRoRepository;
import com.grape.admin.repository.AdminRoleRepository;
import com.grape.admin.repository.AdminUserRoleRelRepository;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
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
import java.util.HashMap;
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

    /** skill_path 白名单：允许字母数字/下划线/中划线/点/斜杠（官方技能现按 agent_type 一层分组），防路径穿越，只读技能目录内文件 */
    private static final Pattern SAFE_SKILL_PATH = Pattern.compile("^[\\w\\-./]+$");
    private static final Pattern SAFE_SKILL_NAME = Pattern.compile("^[a-z0-9-]+$");
    private static final Pattern SEMVER = Pattern.compile("^v?\\d+\\.\\d+(\\.\\d+)?$");
    private static final Set<String> VALID_AGENT_TYPES = Set.of("vod", "issue", "coding", "qa", "sre", "chat");
    // 仅禁二进制可执行/平台原生；文本脚本（.py/.sh 等）放行——官方 Agent Skills 规范 scripts/ 支持（与主项目 validator.py 对齐）
    private static final Set<String> FORBIDDEN_EXTS = Set.of(".bat", ".exe", ".jar", ".class", ".so", ".dll", ".bin");
    private static final Set<String> ALLOWED_EXTS = Set.of(".md", ".txt", ".json", ".yaml", ".yml", ".py", ".sh", ".js", ".ts");
    private static final Set<String> PROJECT_SKILLS = Set.of("pr", "issue", "implementation", "build-debug", "deploy-guard",
            "incident-triage", "changelog-gen", "chat", "cloud-deploy", "im-notify", "agent");
    // 上传大小上限（可配 app.max-zip-bytes，默认 10MB）
    @Value("${app.max-zip-bytes:10485760}")
    private long maxZipBytes;

    private final SkillRoRepository skillRepository;
    private final Path skillsDir;
    private final JdbcTemplate jdbcTemplate;
    private final ObsStorageClient obs;
    private final AdminUserRoleRelRepository roleRelRepository;
    private final AdminRoleRepository roleRepository;

    public SkillService(SkillRoRepository skillRepository,
                        @Value("${app.skills-dir:../../skills}") String skillsDir,
                        @Qualifier("grapeJdbcTemplate") JdbcTemplate jdbcTemplate,
                        ObsStorageClient obs,
                        AdminUserRoleRelRepository roleRelRepository,
                        AdminRoleRepository roleRepository) {
        this.skillRepository = skillRepository;
        this.skillsDir = resolveSkillsDir(skillsDir);
        this.jdbcTemplate = jdbcTemplate;
        this.obs = obs;
        this.roleRelRepository = roleRelRepository;
        this.roleRepository = roleRepository;
    }

    /** 超管判定：当前用户角色含 SUPER_ADMIN（v2.5 更新/删除官方技能权限） */
    private boolean isSuperAdmin(Long userId) {
        if (userId == null) {
            return false;
        }
        try {
            return roleRelRepository.findByUserIdAndDeleted(userId, 0).stream()
                    .map(rel -> roleRepository.findById(rel.getRoleId()))
                    .filter(Optional::isPresent)
                    .map(Optional::get)
                    .anyMatch(r -> "SUPER_ADMIN".equals(r.getCode()));
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * 技能目录解析：优先使用配置/环境变量指定目录；
     * 若不存在则从 jar 位置推导仓库根（<repo>/admin/backend/target/*.jar → <repo>/skills），
     * 保证完整 clone 部署时无论从哪个目录启动都能读到技能文件。
     */
    private Path resolveSkillsDir(String configured) {
        Path p = Paths.get(configured == null ? "../../skills" : configured).toAbsolutePath().normalize();
        if (Files.isDirectory(p)) {
            return p;
        }
        try {
            String loc = SkillService.class.getProtectionDomain().getCodeSource().getLocation().getPath();
            if (loc.startsWith("file:")) {
                loc = loc.substring("file:".length());
            }
            int bang = loc.indexOf("!/");
            if (bang >= 0) {
                loc = loc.substring(0, bang);
            }
            Path jarPath = Paths.get(loc).toAbsolutePath().normalize();
            if (!Files.isDirectory(jarPath)) {
                jarPath = jarPath.getParent(); // jar 文件 → target/ 目录
            }
            // jarPath 为 <repo>/admin/backend/target：target → backend → admin → 仓库根
            Path repoRoot = jarPath.getParent().getParent().getParent();
            Path candidate = repoRoot.resolve("skills");
            if (Files.isDirectory(candidate)) {
                return candidate.normalize();
            }
        } catch (Exception ignored) {
            // 保持配置值
        }
        return p;
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
        Map<String, String> ownerNames = loadOwnerNames(all);
        List<Map<String, Object>> content = all.subList(from, to).stream()
                .map(s -> toMap(s, ownerNames))
                .collect(Collectors.toList());

        Map<String, Long> stats = all.stream()
                .collect(Collectors.groupingBy(s -> s.getScope() == null ? "" : s.getScope(), Collectors.counting()));
        // Agent 类型选项基于全量数据统计（不随当前筛选收窄，避免下拉选项越选越少）
        Map<String, Long> agentStats = skillRepository.findAll().stream()
                .collect(Collectors.groupingBy(s -> s.getAgentType() == null ? "" : s.getAgentType(), Collectors.counting()));
        // 范围分类徽章：全量范围分布（不随筛选收窄）
        Map<String, Long> scopeStats = skillRepository.findAll().stream()
                .collect(Collectors.groupingBy(s -> s.getScope() == null ? "" : s.getScope(), Collectors.counting()));

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("content", content);
        result.put("totalElements", totalElements);
        result.put("totalPages", totalPages);
        result.put("stats", stats);
        result.put("agentStats", agentStats);
        result.put("scopeStats", scopeStats);
        return result;
    }

    private Map<String, String> loadOwnerNames(List<SkillRo> skills) {
        Set<String> userIds = skills.stream()
                .filter(s -> "user".equals(s.getScope()))
                .map(SkillRo::getOwnerId)
                .filter(o -> o != null && o.matches("\\d+"))
                .collect(Collectors.toSet());
        Map<String, String> names = new HashMap<>();
        if (userIds.isEmpty()) {
            return names;
        }
        String inClause = String.join(",", userIds);
        try {
            jdbcTemplate.queryForList("SELECT id, username FROM user_accounts WHERE id IN (" + inClause + ")")
                    .forEach(row -> names.put(String.valueOf(row.get("id")), String.valueOf(row.get("username"))));
        } catch (Exception ignored) {
            // 共享库中 user_accounts 不可用时保持原样（显示数字 id）
        }
        return names;
    }

    private Map<String, Object> toMap(SkillRo s, Map<String, String> ownerNames) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", s.getId());
        m.put("skillName", s.getSkillName());
        m.put("displayName", s.getDisplayName());
        m.put("description", s.getDescription());
        m.put("agentType", s.getAgentType());
        m.put("skillPath", s.getSkillPath());
        m.put("filePath", s.getFilePath());
        m.put("createdBy", s.getCreatedBy());
        m.put("updatedBy", s.getUpdatedBy());
        m.put("scope", s.getScope());
        m.put("ownerId", s.getOwnerId());
        // 个人技能：数字 owner_id 映射为用户名（查不到保持原样）
        m.put("ownerName", "user".equals(s.getScope()) ? ownerNames.get(s.getOwnerId()) : null);
        m.put("isActive", s.getIsActive());
        m.put("updatedAt", s.getUpdatedAt());
        // v2.5：OBS 模式 = file_path 非空（真实存储路径）；兼容存量 skill_path 对象键
        m.put("obsMode", obs.isObjectKey(s.getFilePath()) || obs.isObjectKey(s.getSkillPath()));
        return m;
    }

    /**
     * 仓库/个人技能一键转为官方技能：DB 记录转 official + 文件移动到官方目录。
     * 文件不在本机时仍完成 DB 转换并返回 warning（需在文件所在机器同步）。
     */
    public Map<String, Object> promoteToOfficial(Integer id, String username) throws IOException {
        SkillRo skill = skillRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("技能不存在: id=" + id));
        String scope = skill.getScope();
        if (!"repo".equals(scope) && !"user".equals(scope)) {
            throw new BizException("仅仓库/个人技能可转为官方技能");
        }
        String skillName = skill.getSkillName();
        String ownerId = skill.getOwnerId() == null ? "" : skill.getOwnerId();

        // 唯一键冲突检查：(skill_name, official, '')
        Integer dup = jdbcTemplate.queryForObject(
                "SELECT COUNT(*) FROM skill WHERE skill_name = ? AND scope = 'official' AND owner_id = ''",
                Integer.class, skillName);
        if (dup != null && dup > 0) {
            throw new BizException("官方技能已存在同名技能「" + skillName + "」，无法转换");
        }

        // ===== OBS 分支（N13）：源为 OBS 对象 → copyObject 到 official 固定 key =====
        String srcKey = obs.isObjectKey(skill.getFilePath()) ? skill.getFilePath()
                : obs.isObjectKey(skill.getSkillPath()) ? skill.getSkillPath() : "";
        if (obs.enabled() && !srcKey.isEmpty()) {
            String dstKey = obs.skillKey("official", "", skillName, 1);
            if (!obs.copyObject(srcKey, dstKey)) {
                throw new BizException("OBS 复制技能失败: " + srcKey + " -> " + dstKey);
            }
            // v2.5：file_path=目标对象键（真实存储路径）；updated_by=操作者；created_by 保留
            jdbcTemplate.update("UPDATE skill SET scope='official', owner_id='', file_path=?, updated_by=? WHERE id=?",
                    dstKey, username, id);
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("message", "技能「" + skillName + "」已转为官方技能");
            return result;
        }
        // 文件移动：源目录（repo/user）→ 官方目录 skills/<skill_name>
        Path targetDir = skillsDir.resolve(skillName).normalize();
        if (Files.exists(targetDir)) {
            throw new BizException("官方技能目录已存在: skills/" + skillName + "，无法转换");
        }
        Path srcDir = "repo".equals(scope)
                ? skillsDir.resolve("repo").resolve(ownerId).resolve(skillName).normalize()
                : skillsDir.resolve("custom").resolve(ownerId).resolve(skillName).normalize();
        String warning = null;
        if (Files.isDirectory(srcDir)) {
            try {
                copyRecursively(srcDir, targetDir);
                deleteRecursively(srcDir);
            } catch (IOException e) {
                throw new BizException("技能文件移动失败: " + e.getMessage());
            }
        } else {
            warning = "技能文件不在当前服务器（" + srcDir + "），已转换记录；请在该文件所在机器同步到官方目录 skills/" + skillName;
        }

        // DB 转换 + 自动转存（B 方案一致）：本地技能转官方后打包上传 OBS official 固定 key；
        // 文件不在本机（warning）→ 跳过转存（file_path 留空，提示人工同步）
        String newFilePath = "";
        if (obs.enabled() && warning == null) {
            String objectKey = obs.skillKey("official", "", skillName, 1);
            try {
                byte[] zipBytes = obs.normalizeSkillZip(targetDir, skillName);
                obs.saveBytes(objectKey, zipBytes);
            } catch (java.io.IOException e) {
                throw new BizException("技能打包失败：" + skillName + " -> " + e.getMessage());
            }
            newFilePath = objectKey;
        }
        jdbcTemplate.update("UPDATE skill SET scope = 'official', owner_id = '', file_path = ?, updated_by = ? WHERE id = ?",
                newFilePath, username, id);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("message", "技能「" + skillName + "」已转为官方技能");
        if (warning != null) {
            result.put("warning", warning);
        }
        return result;
    }

    /**
     * 官方技能转集群模式：本地目录打包上传 OBS + DB skill_path 回写。
     * 仅官方技能；幂等（已转返回现状）；不可回退。
     * 与主服务 POST /api/skills/{name}/to-cluster 同机制（对象键规范一致，共享库同步可见）。
     */
    public Map<String, Object> toClusterMode(Integer id, String username) throws IOException {
        SkillRo skill = skillRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("技能不存在: id=" + id));
        if (!"official".equals(skill.getScope())) {
            throw new BizException("仅官方技能支持转集群模式");
        }
        if (obs.isObjectKey(skill.getFilePath()) || obs.isObjectKey(skill.getSkillPath())) {
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("message", "技能 " + skill.getSkillName() + " 已是集群模式");
            r.put("obsMode", true);
            return r;
        }
        // 本地官方技能目录（uploadSkill 落盘处 skillsDir/{skillName}）
        String rel = (skill.getSkillPath() == null || skill.getSkillPath().isBlank())
                ? skill.getSkillName() : skill.getSkillPath();
        Path skillDir = skillsDir.resolve(rel).normalize();
        if (!skillDir.startsWith(skillsDir) || !Files.isDirectory(skillDir)) {
            throw new NotFoundException("官方技能目录不存在: " + skillDir);
        }
        String objectKey = obs.skillKey("official", "", skill.getSkillName(), 1);
        try {
            byte[] zipBytes = obs.normalizeSkillZip(skillDir, skill.getSkillName());
            obs.saveBytes(objectKey, zipBytes);
        } catch (java.io.IOException e) {
            throw new BizException("技能打包失败：" + skill.getSkillName() + " -> " + e.getMessage());
        }
        // v2.5：file_path=对象键（真实存储路径）；updated_by=操作者（created_by 保留）
        jdbcTemplate.update("UPDATE skill SET file_path = ?, updated_by = ? WHERE id = ?",
                objectKey, username, id);

        Map<String, Object> r = new LinkedHashMap<>();
        r.put("message", "技能 " + skill.getSkillName() + " 已转存至 OBS");
        r.put("obsMode", true);
        r.put("objectKey", objectKey);
        return r;
    }

    /**
     * 官方技能更新（v2.5：独立接口，超管 OR 创建者本人）：
     * 校验 zip 包内目录名 == 技能名；内容落盘覆盖 + OBS 同步（file_path 保留）。
     */
    public Map<String, Object> updateSkill(Integer id, InputStream zipStream, long size,
                                           String agentType, String username, Long userId) {
        SkillRo skill = skillRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("技能不存在: id=" + id));
        if (!"official".equals(skill.getScope())) {
            throw new BizException("仅官方技能支持更新");
        }
        // 权限：超管 OR 创建者本人
        String createdBy = skill.getCreatedBy() == null ? "" : skill.getCreatedBy();
        if (!isSuperAdmin(userId) && (createdBy.isEmpty() || !createdBy.equals(username))) {
            throw new BizException("无更新权限：仅超级管理员或创建者本人可更新该官方技能");
        }
        if (agentType == null || agentType.isBlank()) {
            throw new BizException("必须选择技能类型（agent_type）");
        }
        if (!VALID_AGENT_TYPES.contains(agentType)) {
            throw new BizException("技能类型不合法，可选: " + VALID_AGENT_TYPES.stream().sorted().collect(Collectors.joining(", ")));
        }
        if (size > maxZipBytes) {
            throw new BizException("Skill 文件不能超过 10MB");
        }

        Path tmpDir;
        try {
            tmpDir = Files.createTempDirectory("skill-update-");
        } catch (IOException e) {
            throw new BizException("更新失败: " + e.getMessage());
        }
        try {
            // 解压 ZIP 到临时目录（与 uploadSkill 同逻辑）
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

            // 目录名校验：包内目录名必须与技能名一致
            if (!skillName.equals(skill.getSkillName())) {
                throw new BizException("技能包内目录名与技能名不匹配：包内目录「" + skillName
                        + "」≠ 技能名「" + skill.getSkillName() + "」");
            }
            // SKILL.md 校验（与主项目 validator.py 规则一致）
            List<String> errors = validateCustomSkill(skillDir, skillName);
            if (!errors.isEmpty()) {
                throw new BizException("Skill 校验失败: " + String.join("; ", errors));
            }
            // 落盘覆盖 skills/{skillName}/
            Path targetDir = skillsDir.resolve(skillName).normalize();
            if (!targetDir.startsWith(skillsDir)) {
                throw new BizException("技能路径非法");
            }
            Files.createDirectories(targetDir.getParent());
            if (Files.exists(targetDir)) {
                deleteRecursively(targetDir);
            }
            copyRecursively(skillDir, targetDir);
            // OBS 同步（v2.5 B 方案）：官方技能更新【始终上传 OBS】——
            // 已转存（file_path 对象键）覆盖对象；未转存自动转存（file_path 回写对象键）
            String filePath = skill.getFilePath();
            String newFilePath = filePath;
            if (obs.enabled()) {
                String objectKey = obs.isObjectKey(filePath) ? filePath
                        : obs.skillKey("official", "", skillName, 1);
                try {
                    byte[] zipBytes = obs.normalizeSkillZip(skillDir, skillName);
                    obs.saveBytes(objectKey, zipBytes);
                } catch (java.io.IOException e) {
                    throw new BizException("技能打包失败：" + skillName + " -> " + e.getMessage());
                }
                if (!obs.isObjectKey(filePath)) {
                    newFilePath = objectKey;  // 未转存 → 自动转存
                }
            }
            // DB：updated_by=操作者（created_by 保留）；自动转存时回写 file_path
            if (!newFilePath.equals(filePath)) {
                jdbcTemplate.update("UPDATE skill SET file_path=?, updated_by=?, updated_at=NOW() WHERE id=?",
                        newFilePath, username, id);
            } else {
                jdbcTemplate.update("UPDATE skill SET updated_by=?, updated_at=NOW() WHERE id=?",
                        username, id);
            }
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("message", "技能「" + skillName + "」更新成功");
            r.put("skillName", skillName);
            return r;
        } catch (BizException e) {
            throw e;
        } catch (Exception e) {
            throw new BizException("更新失败: " + e.getMessage());
        } finally {
            deleteRecursively(tmpDir);
        }
    }

    /**
     * 官方技能删除（v2.5）：超管 OR 创建者本人可删；系统内置（created_by=''）永不可删。
     * 动作：OBS 对象（file_path 非空时）单对象删除 + DB 记录删除。
     */
    public Map<String, Object> deleteSkill(Integer id, String username, Long userId) {
        SkillRo skill = skillRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("技能不存在: id=" + id));
        if (!"official".equals(skill.getScope())) {
            throw new BizException("仅官方技能支持在管理台删除（个人/仓库技能请在主服务侧管理）");
        }
        String createdBy = skill.getCreatedBy() == null ? "" : skill.getCreatedBy();
        if (createdBy.isEmpty()) {
            throw new BizException("系统内置技能不可删除（部署资产）");
        }
        if (!isSuperAdmin(userId) && !createdBy.equals(username)) {
            throw new BizException("无删除权限：仅超级管理员或创建者本人可删除该官方技能");
        }
        // OBS 对象删除（file_path 真实存储路径；兼容存量 skill_path 对象键）
        String objectKey = obs.isObjectKey(skill.getFilePath()) ? skill.getFilePath()
                : obs.isObjectKey(skill.getSkillPath()) ? skill.getSkillPath() : "";
        if (!objectKey.isEmpty() && obs.enabled()) {
            obs.deleteObject(objectKey);
        }
        skillRepository.deleteById(id);
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("message", "技能「" + skill.getSkillName() + "」已删除");
        return r;
    }

    /**
     * 技能包下载临时地址：OBS 对象 → 临时签名 URL；未转官方技能 → 400。
     */
    public Map<String, Object> downloadUrl(Integer id) {
        SkillRo skill = skillRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("技能不存在: id=" + id));
        if (obs.isObjectKey(skill.getFilePath()) || obs.isObjectKey(skill.getSkillPath())) {
            String objectKey = obs.isObjectKey(skill.getFilePath()) ? skill.getFilePath() : skill.getSkillPath();
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("url", obs.temporaryUrl(objectKey));
            r.put("expires_in", obs.urlExpires());
            return r;
        }
        if ("official".equals(skill.getScope())) {
            throw new BizException("官方技能为部署内置，未转集群模式不提供下载");
        }
        throw new BizException("技能对象不存在（历史数据请先迁移）");
    }

    public Map<String, Object> getSkillContent(Integer id) {
        SkillRo skill = skillRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("技能不存在: id=" + id));
        String skillPath = skill.getSkillPath();
        // ===== OBS 分支（N14）：file_path 为 OBS 对象键（v2.5 真实存储路径）；
        // 兼容存量 skill_path 对象键 → 下载 zip 提取 SKILL.md =====
        String filePath = skill.getFilePath();
        String objectKey = obs.isObjectKey(filePath) ? filePath
                : obs.isObjectKey(skillPath) ? skillPath : "";
        if (obs.enabled() && !objectKey.isEmpty()) {
            byte[] zip = obs.readBytes(objectKey);
            String content = obs.extractSkillMd(zip, skill.getSkillName());
            Map<String, Object> result = new LinkedHashMap<>();
            result.put("id", skill.getId());
            result.put("skillName", skill.getSkillName());
            result.put("displayName", skill.getDisplayName());
            result.put("scope", skill.getScope());
            result.put("content", content);
            return result;
        }
        if (skillPath == null || skillPath.isBlank() || !SAFE_SKILL_PATH.matcher(skillPath).matches()) {
            throw new NotFoundException("技能路径不合法: " + skillPath);
        }
        Path md = skillsDir.resolve(skillPath).resolve("SKILL.md").normalize();
        if (!md.startsWith(skillsDir) || !Files.isRegularFile(md)) {
            throw new NotFoundException("SKILL.md 文件不存在：技能文件可能存储于上传者所在服务器，本环境不可读（" + skillPath + "）");
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
        // 参数校验（与主项目 grape upload 一致；official 供系统总看板维护官方技能库）
        if (scope == null || !(scope.equals("user") || scope.equals("repo") || scope.equals("official"))) {
            throw new BizException("scope 必须为 official、user 或 repo");
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
        if (size > maxZipBytes) {
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

            // v2.5 R1：官方技能同名禁止上传（scope='official' 已存在同名 → 400）
            // 更新走独立接口 POST /{id}/update（超管/创建者权限 + 目录名校验）
            if ("official".equals(scope)) {
                Integer dup = jdbcTemplate.queryForObject(
                        "SELECT COUNT(*) FROM skill WHERE skill_name = ? AND scope = 'official'",
                        Integer.class, skillName);
                if (dup != null && dup > 0) {
                    throw new BizException("官方技能已存在同名「" + skillName + "」，请使用更新功能修改");
                }
            }

            // SKILL.md 校验（与主项目 validator.py 规则一致）
            List<String> errors = validateCustomSkill(skillDir, skillName);
            if (!errors.isEmpty()) {
                throw new BizException("Skill 校验失败: " + String.join("; ", errors));
            }
            Map<String, Object> fm = parseFrontmatter(skillDir);
            String description = String.valueOf(fm.getOrDefault("description", "")).trim();

            // ===== OBS 分支（N12）：OBS 启用 → 规范化重打包上传 OBS =====
            // v2.7 A 方案：official 也走 OBS（上传即转存——file_path=对象键，
            // obsMode=True）；与 user/repo 一致；落盘分支仅 OBS 未启用时使用
            if (obs.enabled()) {
                String ownerId;
                if (scope.equals("repo")) {
                    ownerId = repo.trim();
                } else if (scope.equals("official")) {
                    ownerId = "";   // official owner_id=''
                } else {
                    ownerId = username;
                }
                // v2.4 固定 key 覆盖模型：无版本概念，putObject 同 key 原子覆盖
                // v2.5：file_path=对象键（真实存储路径）+ created_by/updated_by=当前用户
                String objectKey = obs.skillKey(scope, ownerId, skillName, 1);
                try {
                    byte[] zipBytes = obs.normalizeSkillZip(skillDir, skillName);
                    obs.saveBytes(objectKey, zipBytes);
                } catch (java.io.IOException e) {
                    throw new BizException("技能打包失败：" + skillName + " -> " + e.getMessage());
                }
                skillRepository.deleteBySkillNameAndScopeAndOwnerId(skillName, scope, ownerId);
                skillRepository.flush();
                jdbcTemplate.update(
                        "INSERT INTO skill (skill_name, display_name, description, agent_type, skill_path, file_path, scope, owner_id, created_by, updated_by, is_active) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                        skillName, skillName, description, agentType, skillName, objectKey, scope, ownerId, username, username);
                Map<String, Object> result = new LinkedHashMap<>();
                result.put("message", "技能 " + skillName + " 上传成功");
                result.put("skillName", skillName);
                return result;
            }
            // 落盘：official → skills/<skill_name>；repo → skills/repo/<repo_key>/<skill_name>；user → skills/custom/<username>/<skill_name>
            Path targetDir;
            String ownerId;
            if (scope.equals("official")) {
                ownerId = "";
                targetDir = skillsDir.resolve(skillName).normalize();
            } else if (scope.equals("repo")) {
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
            // v2.5：upload 仅新增（官方同名已在上方 400 拒绝，不存在覆盖场景）；
            // created_by/updated_by=当前登录用户
            skillRepository.deleteBySkillNameAndScopeAndOwnerId(skillName, scope, ownerId);
            skillRepository.flush();
            skillRepository.insertSkill(skillName, skillName, description, agentType,
                    skillName, "", scope, ownerId, username, username);

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
        String version = String.valueOf(fm.getOrDefault("version", "")).trim();
        // version 为可选字段（官方 Agent Skills 规范未强制）：缺失/空不报错，有则校验语义化版本号
        if (!version.isEmpty() && !SEMVER.matcher(version).matches()) {
            errors.add("version 字段必须符合语义化版本号格式（x.y.z）");
        }
        String desc = String.valueOf(fm.getOrDefault("description", "")).trim();
        if (desc.isEmpty()) {
            errors.add("description 字段不能为空");
        } else if (desc.length() > 500) {
            errors.add("description 字段不超过 500 字");
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
        m.put("obsMode", obs.isObjectKey(s.getSkillPath()));
        return m;
    }

    private static boolean containsIgnoreCase(String haystack, String needle) {
        return haystack != null && haystack.toLowerCase().contains(needle.toLowerCase());
    }
}
