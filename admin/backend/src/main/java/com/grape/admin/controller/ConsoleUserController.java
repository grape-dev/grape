package com.grape.admin.controller;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;

import java.util.*;

/**
 * Grape 控制台用户管理（user_accounts 表，托管仓库的登录用户）
 */
@RestController
@RequestMapping("/api/admin/console-users")
public class ConsoleUserController {

    private final JdbcTemplate grapeJdbc;

    public ConsoleUserController(@Qualifier("grapeJdbcTemplate") JdbcTemplate grapeJdbc) {
        this.grapeJdbc = grapeJdbc;
    }

    @GetMapping
    public Map<String, Object> list(@RequestParam(required = false, defaultValue = "0") int page,
                                    @RequestParam(required = false, defaultValue = "10") int size,
                                    @RequestParam(required = false) String keyword,
                                    @RequestParam(required = false) String credentialFilter) {
        Map<String, Object> result = new LinkedHashMap<>();

        StringBuilder where = new StringBuilder(" WHERE 1=1");
        List<Object> params = new ArrayList<>();
        if (keyword != null && !keyword.isBlank()) {
            where.append(" AND (username LIKE ? OR nickname LIKE ?)");
            params.add("%" + keyword + "%");
            params.add("%" + keyword + "%");
        }
        if (credentialFilter != null && !credentialFilter.isBlank()) {
            if ("gitcode".equals(credentialFilter)) {
                where.append(" AND gitcode_api_token IS NOT NULL AND gitcode_api_token<>''");
            } else if ("huawei".equals(credentialFilter)) {
                where.append(" AND huawei_cloud_ak IS NOT NULL AND huawei_cloud_ak<>''");
            } else if ("feishu".equals(credentialFilter)) {
                where.append(" AND feishu_app_id IS NOT NULL AND feishu_app_id<>''");
            } else if ("none".equals(credentialFilter)) {
                where.append(" AND (gitcode_api_token IS NULL OR gitcode_api_token='') AND (huawei_cloud_ak IS NULL OR huawei_cloud_ak='') AND (feishu_app_id IS NULL OR feishu_app_id='')");
            }
        }

        long total = 0;
        try {
            total = grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts" + where, Long.class, params.toArray());
        } catch (Exception e) { /* ignore */ }

        List<Map<String, Object>> list = new ArrayList<>();
        try {
            String sql = "SELECT id, username, nickname, gitcode_id, created_at, "
                    + "huawei_cloud_ak IS NOT NULL AND huawei_cloud_ak<>'' as has_huawei_ak, "
                    + "feishu_app_id IS NOT NULL AND feishu_app_id<>'' as has_feishu, "
                    + "gitcode_api_token IS NOT NULL AND gitcode_api_token<>'' as has_gitcode_token "
                    + "FROM user_accounts" + where + " ORDER BY id DESC LIMIT ? OFFSET ?";
            params.add(size);
            params.add(page * size);
            grapeJdbc.query(sql, rs -> {
                Map<String, Object> u = new LinkedHashMap<>();
                u.put("id", rs.getInt("id"));
                u.put("username", rs.getString("username"));
                u.put("nickname", rs.getString("nickname"));
                u.put("gitcodeId", rs.getString("gitcode_id"));
                u.put("createdAt", rs.getString("created_at"));
                u.put("hasHuaweiAk", rs.getBoolean("has_huawei_ak"));
                u.put("hasFeishu", rs.getBoolean("has_feishu"));
                u.put("hasGitcodeToken", rs.getBoolean("has_gitcode_token"));
                list.add(u);
            }, params.toArray());
        } catch (Exception e) { /* ignore */ }

        // 全量凭证统计（不受筛选影响）
        Map<String, Object> credentialStats = new LinkedHashMap<>();
        try {
            credentialStats.put("total", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts", Long.class));
            credentialStats.put("hasGitcode", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE gitcode_api_token IS NOT NULL AND gitcode_api_token<>''", Long.class));
            credentialStats.put("hasHuawei", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE huawei_cloud_ak IS NOT NULL AND huawei_cloud_ak<>''", Long.class));
            credentialStats.put("hasFeishu", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE feishu_app_id IS NOT NULL AND feishu_app_id<>''", Long.class));
            credentialStats.put("noCredential", grapeJdbc.queryForObject("SELECT COUNT(*) FROM user_accounts WHERE (gitcode_api_token IS NULL OR gitcode_api_token='') AND (huawei_cloud_ak IS NULL OR huawei_cloud_ak='') AND (feishu_app_id IS NULL OR feishu_app_id='')", Long.class));
        } catch (Exception e) { /* ignore */ }

        result.put("content", list);
        result.put("totalElements", total);
        result.put("totalPages", (total + size - 1) / size);
        result.put("page", page);
        result.put("size", size);
        result.put("credentialStats", credentialStats);
        return result;
    }

    @GetMapping("/{id}")
    public Map<String, Object> detail(@PathVariable int id) {
        Map<String, Object> info = new LinkedHashMap<>();
        try {
            grapeJdbc.query("SELECT id, username, nickname, gitcode_id, created_at, "
                    + "huawei_cloud_ak IS NOT NULL AND huawei_cloud_ak<>'' as has_huawei_ak, "
                    + "huawei_cloud_sk IS NOT NULL AND huawei_cloud_sk<>'' as has_huawei_sk, "
                    + "feishu_app_id IS NOT NULL AND feishu_app_id<>'' as has_feishu, "
                    + "feishu_app_secret IS NOT NULL AND feishu_app_secret<>'' as has_feishu_secret, "
                    + "gitcode_api_token IS NOT NULL AND gitcode_api_token<>'' as has_gitcode_token, "
                    + "CODEARTS_CLI_AK IS NOT NULL AND CODEARTS_CLI_AK<>'' as has_codearts_ak, "
                    + "CODEARTS_CLI_SK IS NOT NULL AND CODEARTS_CLI_SK<>'' as has_codearts_sk, "
                    + "gitcode_publish_repo, github_publish_repo, last_scene "
                    + "FROM user_accounts WHERE id=?", rs -> {
                info.put("id", rs.getInt("id"));
                info.put("username", rs.getString("username"));
                info.put("nickname", rs.getString("nickname"));
                info.put("gitcodeId", rs.getString("gitcode_id"));
                info.put("createdAt", rs.getString("created_at"));
                info.put("hasHuaweiAk", rs.getBoolean("has_huawei_ak"));
                info.put("hasHuaweiSk", rs.getBoolean("has_huawei_sk"));
                info.put("hasFeishu", rs.getBoolean("has_feishu"));
                info.put("hasFeishuSecret", rs.getBoolean("has_feishu_secret"));
                info.put("hasGitcodeToken", rs.getBoolean("has_gitcode_token"));
                info.put("hasCodeartsAk", rs.getBoolean("has_codearts_ak"));
                info.put("hasCodeartsSk", rs.getBoolean("has_codearts_sk"));
                info.put("gitcodePublishRepo", rs.getString("gitcode_publish_repo"));
                info.put("githubPublishRepo", rs.getString("github_publish_repo"));
                info.put("lastScene", rs.getString("last_scene"));
            }, id);
        } catch (Exception e) { /* ignore */ }
        return info;
    }
}
