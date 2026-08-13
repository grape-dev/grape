package com.grape.admin.config;

import com.zaxxer.hikari.HikariDataSource;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.orm.jpa.JpaTransactionManager;
import org.springframework.transaction.PlatformTransactionManager;

import javax.persistence.EntityManagerFactory;
import javax.sql.DataSource;

/**
 * 双数据源配置（grape_admin_db 独立库拆分，见 admin/docs/admin-db-split-design.md v1.0）
 *
 * - adminDataSource（@Primary）：admin 管理台自有表（admin_* 共 9 张），连 grape_admin_db
 * - grapeDataSource：grape 业务表（agent_runtimes/agent_tasks/... 共 10 张），连 grape_test_db
 * - 连接地址/账号/密码一律来自环境变量（ADMIN_DB_* / GRAPE_DB_*，见 admin.env），
 *   代码与配置文件均不写死；未配置时 Hikari 启动即失败（fail-fast，防漏配）
 * - adminJdbcTemplate / grapeJdbcTemplate：均不标 @Primary（P3），注入点必须显式 @Qualifier，
 *   漏改 = NoUniqueBeanDefinitionException 启动失败（fail-fast，防静默查错库）
 * - adminTransactionManager（@Primary）：@Transactional 默认绑定；grape 库操作需要事务时
 *   必须显式 @Transactional("grapeTransactionManager")（防事务错绑静默失效）
 */
@Configuration
public class DataSourceConfig {

    @Bean
    @Primary
    public DataSource adminDataSource(
            @Value("${ADMIN_DB_URL:}") String url,
            @Value("${ADMIN_DB_USER:}") String user,
            @Value("${ADMIN_DB_PASSWORD:}") String password) {
        return buildHikari(url, user, password);
    }

    @Bean
    public DataSource grapeDataSource(
            @Value("${GRAPE_DB_URL:}") String url,
            @Value("${GRAPE_DB_USER:}") String user,
            @Value("${GRAPE_DB_PASSWORD:}") String password) {
        return buildHikari(url, user, password);
    }

    private HikariDataSource buildHikari(String url, String user, String password) {
        HikariDataSource ds = new HikariDataSource();
        ds.setJdbcUrl(url);
        ds.setUsername(user);
        ds.setPassword(password);
        ds.setDriverClassName("com.mysql.cj.jdbc.Driver");
        ds.setMaximumPoolSize(10);
        return ds;
    }

    @Bean
    public JdbcTemplate adminJdbcTemplate(@Qualifier("adminDataSource") DataSource dataSource) {
        return new JdbcTemplate(dataSource);
    }

    @Bean
    public JdbcTemplate grapeJdbcTemplate(@Qualifier("grapeDataSource") DataSource dataSource) {
        return new JdbcTemplate(dataSource);
    }

    @Bean
    @Primary
    public PlatformTransactionManager adminTransactionManager(
            @Qualifier("adminEntityManagerFactory") EntityManagerFactory emf) {
        return new JpaTransactionManager(emf);
    }

    @Bean
    public PlatformTransactionManager grapeTransactionManager(
            @Qualifier("grapeEntityManagerFactory") EntityManagerFactory emf) {
        return new JpaTransactionManager(emf);
    }
}
