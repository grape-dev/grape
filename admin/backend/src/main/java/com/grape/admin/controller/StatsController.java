package com.grape.admin.controller;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowCallbackHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 运营总览统计接口。直读共享库（agent_runtimes / agent_tasks / events 等）聚合 KPI 与图表分布。
 * 双数据源（admin-db-split v1.0）：grape 业务表走 grapeJdbcTemplate，admin_users 走 adminJdbcTemplate。
 */
@RestController
@RequestMapping("/api/admin/stats")
public class StatsController {

    private final JdbcTemplate grapeJdbc;
    private final JdbcTemplate adminJdbc;

    public StatsController(@Qualifier("grapeJdbcTemplate") JdbcTemplate grapeJdbc,
                           @Qualifier("adminJdbcTemplate") JdbcTemplate adminJdbc) {
        this.grapeJdbc = grapeJdbc;
        this.adminJdbc = adminJdbc;
    }

    /** KPI 汇总卡片数据 */
    @GetMapping("/overview")
    public Map<String, Object> overview() {
        Map<String, Object> data = new LinkedHashMap<>();

        // 设备：总数 / 在线
        long totalDevices = count("SELECT COUNT(*) FROM agent_runtimes");
        long onlineDevices = count("SELECT COUNT(*) FROM agent_runtimes WHERE status='online'");
        data.put("totalDevices", totalDevices);
        data.put("onlineDevices", onlineDevices);

        // 引擎：总数 / 启用
        data.put("engineTotal", count("SELECT COUNT(*) FROM agent_type"));
        data.put("engineEnabled", count("SELECT COUNT(*) FROM agent_type WHERE enable=1"));

        // 凭据：总数 / 有效
        data.put("credentialTotal", count("SELECT COUNT(*) FROM runtime_credentials"));
        data.put("credentialActive", count("SELECT COUNT(*) FROM runtime_credentials WHERE status='active'"));

        // 任务：总数 / 今日新增 / 已完成 / 成功率 / 最近完成时间
        long taskTotal = count("SELECT COUNT(*) FROM agent_tasks");
        data.put("taskTotal", taskTotal);
        data.put("taskToday", count("SELECT COUNT(*) FROM agent_tasks WHERE SUBSTR(created_at,1,10)=SUBSTR(NOW(),1,10)"));
        long completed = count("SELECT COUNT(*) FROM agent_tasks WHERE status='completed'");
        data.put("taskCompleted", completed);
        data.put("taskSuccessRate", taskTotal > 0 ? String.format("%.1f%%", completed * 100.0 / taskTotal) : "0.0%");
        data.put("lastTaskAt", grapeJdbc.query("SELECT MAX(completed_at) FROM agent_tasks WHERE completed_at IS NOT NULL AND completed_at<>''",
                rs -> rs.next() ? rs.getString(1) : null));

        // 托管仓库
        data.put("repoTotal", count("SELECT COUNT(*) FROM managed_repos"));

        // 用户：管理台用户（admin 库）/ 平台用户（grape 库）
        data.put("adminUserTotal", adminJdbc.queryForObject("SELECT COUNT(*) FROM admin_users", Long.class));
        data.put("platformUserTotal", count("SELECT COUNT(*) FROM user_accounts"));

        return data;
    }

    /** 图表分布数据 */
    @GetMapping("/dashboard")
    public Map<String, Object> dashboard() {
        Map<String, Object> data = new LinkedHashMap<>();

        // 任务状态分布（柱状图）
        data.put("taskStatusDistribution", groupCount(
                "SELECT COALESCE(status,'未知'), COUNT(*) FROM agent_tasks GROUP BY status"));

        // 事件类型分布（环形图）
        data.put("eventTypeDistribution", groupCount(
                "SELECT COALESCE(event_type,'未知'), COUNT(*) FROM events GROUP BY event_type ORDER BY 2 DESC"));

        // 近 14 天事件趋势（折线图，按天）
        data.put("eventTrend", groupCount(
                "SELECT SUBSTR(created_at,1,10), COUNT(*) FROM events " +
                "WHERE created_at IS NOT NULL AND created_at<>'' " +
                "GROUP BY SUBSTR(created_at,1,10) ORDER BY 1 DESC LIMIT 14"));

        // 设备状态分布（饼图）
        data.put("deviceStatusDistribution", groupCount(
                "SELECT COALESCE(status,'未知'), COUNT(*) FROM agent_runtimes GROUP BY status"));

        // 凭据状态分布（饼图）
        data.put("credentialStatusDistribution", groupCount(
                "SELECT COALESCE(status,'未知'), COUNT(*) FROM runtime_credentials GROUP BY status"));

        // 仓库任务分布 Top 10（按 issue 数量统计，对应"托管仓库"卡片）
        data.put("repoEventDistribution", groupCount(
                "SELECT COALESCE(repo_key,'未知'), COUNT(DISTINCT issue_id) FROM events " +
                "WHERE repo_key IS NOT NULL AND repo_key<>'' AND issue_id IS NOT NULL AND issue_id<>'' " +
                "GROUP BY repo_key ORDER BY 2 DESC LIMIT 10"));

        // 用户构成分布（控制台用户 vs 管理台用户，对应"用户"卡片）
        LinkedHashMap<String, Object> userComp = new LinkedHashMap<>();
        userComp.put("工作台用户", count("SELECT COUNT(*) FROM user_accounts"));
        data.put("userCompositionDistribution", userComp);

        // 引擎启停分布
        data.put("engineEnabledDistribution", groupCount(
                "SELECT CASE WHEN enable=1 THEN '启用' ELSE '停用' END, COUNT(*) FROM agent_type GROUP BY enable"));

        return data;
    }

    private long count(String sql) {
        Long v = grapeJdbc.queryForObject(sql, Long.class);
        return v != null ? v : 0L;
    }

    private Map<String, Object> groupCount(String sql) {
        Map<String, Object> m = new LinkedHashMap<>();
        grapeJdbc.query(sql, new RowCallbackHandler() {
            @Override
            public void processRow(ResultSet rs) throws SQLException {
                m.put(rs.getString(1), rs.getLong(2));
            }
        });
        return m;
    }
}
