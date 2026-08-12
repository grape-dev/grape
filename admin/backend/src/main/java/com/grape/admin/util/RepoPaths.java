package com.grape.admin.util;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * 仓库根目录定位工具：技能库/配置等位于 <repo>/skills、<repo>/configs。
 * 支持 SKILLS_DIR/配置显式指定；未指定且默认相对路径不存在时，从 jar 位置推导
 * （jar 在 <repo>/admin/backend/target/ 下，向上 4 级为仓库根）。
 */
public final class RepoPaths {

    private RepoPaths() {
    }

    /**
     * 解析仓库根目录。
     *
     * @param configuredSkillsDir 配置的 skills 目录（可为 null，默认 ../../skills）
     * @return 仓库根绝对路径（存在则优先）
     */
    public static Path resolveRepoRoot(String configuredSkillsDir) {
        Path p = Paths.get(configuredSkillsDir == null ? "../../skills" : configuredSkillsDir)
                .toAbsolutePath().normalize();
        if (Files.isDirectory(p)) {
            return p.getParent().normalize(); // skills 目录的父级 = 仓库根
        }
        try {
            String loc = RepoPaths.class.getProtectionDomain().getCodeSource().getLocation().getPath();
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
            return jarPath.getParent().getParent().getParent().normalize();
        } catch (Exception ignored) {
            // 保持配置值推导
        }
        return p.getParent().normalize();
    }
}
