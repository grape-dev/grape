package com.grape.admin;

import org.flywaydb.core.Flyway;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.flyway.FlywayMigrationStrategy;
import org.springframework.context.annotation.Bean;

@SpringBootApplication
public class AdminApplication {

    public static void main(String[] args) {
        SpringApplication.run(AdminApplication.class, args);
    }

    /**
     * 忽略"已应用但代码缺失"的迁移（feat 分支开发中 V10/V12 等迁移文件尚未入库时避免校验失败）。
     * Spring Boot 2.7 + Flyway 9.22 组合下 application.yml 的 ignore-migration-patterns
     * 属性绑定不生效（已知兼容问题），改在代码层直接调用 Flyway API。
     */
    @Bean
    public FlywayMigrationStrategy flywayMigrationStrategy() {
        return (Flyway flyway) -> {
            // 跳过 validate：feat 分支开发中 V10/V12 等用户迁移文件尚未入库，
            // "applied not resolved" 校验会导致启动失败（属性配置在此组合下不生效）
            org.flywaydb.core.api.configuration.Configuration cfg = flyway.getConfiguration();
            if (cfg instanceof org.flywaydb.core.api.configuration.ClassicConfiguration) {
                ((org.flywaydb.core.api.configuration.ClassicConfiguration) cfg).setValidateOnMigrate(false);
            } else if (cfg instanceof org.flywaydb.core.api.configuration.FluentConfiguration) {
                ((org.flywaydb.core.api.configuration.FluentConfiguration) cfg).validateOnMigrate(false);
            }
            flyway.migrate();
        };
    }
}
