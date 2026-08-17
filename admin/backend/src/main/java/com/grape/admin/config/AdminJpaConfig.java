package com.grape.admin.config;

import org.hibernate.jpa.HibernatePersistenceProvider;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.autoconfigure.orm.jpa.JpaProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.FilterType;
import org.springframework.context.annotation.Primary;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.orm.jpa.LocalContainerEntityManagerFactoryBean;
import org.springframework.orm.jpa.persistenceunit.MutablePersistenceUnitInfo;
import org.springframework.orm.jpa.persistenceunit.PersistenceUnitManager;
import org.springframework.orm.jpa.vendor.HibernateJpaVendorAdapter;

import javax.persistence.spi.PersistenceUnitInfo;
import javax.sql.DataSource;
import java.util.HashMap;
import java.util.Map;

/**
 * admin 持久化单元（admin 自有表）：实体精确注册，排除 entity.readonly 子包。
 *
 * - @EnableJpaRepositories excludeFilters（P4）：repository.readonly 子包由 GrapeJpaConfig 接管，
 *   避免两个持久化单元重复注册仓库
 * - 实体扫描（P4）：admin EMF 手动注册 8 个 Admin* 实体类（MutablePersistenceUnitInfo），
 *   不用包扫描——com.grape.admin.entity 的子包 entity.readonly 无法用包字符串排除，
 *   Spring Boot 2.7 又无 setExcludedPackages API，精确注册是唯一干净的排除方式；
 *   新增 admin 实体时在此数组补充（集中管理，见设计文档 §4.2）
 */
@Configuration
@EnableJpaRepositories(
        basePackages = "com.grape.admin.repository",
        excludeFilters = @ComponentScan.Filter(
                type = FilterType.REGEX,
                pattern = "com\\.grape\\.admin\\.repository\\.readonly\\..*"),
        entityManagerFactoryRef = "adminEntityManagerFactory",
        transactionManagerRef = "adminTransactionManager")
public class AdminJpaConfig {

    /** admin 自有实体类清单（与 admin_* 表一一对应；AdminAuditBase 为 @MappedSuperclass 基类不注册） */
    private static final String[] ADMIN_ENTITY_CLASSES = {
            "com.grape.admin.entity.AdminUser",
            "com.grape.admin.entity.AdminRole",
            "com.grape.admin.entity.AdminPermissionMenu",
            "com.grape.admin.entity.AdminRoleMenuRel",
            "com.grape.admin.entity.AdminUserRoleRel",
            "com.grape.admin.entity.AdminAuditLog",
            "com.grape.admin.entity.AdminAiHistory",
            "com.grape.admin.entity.AdminAiTemplate",
            "com.grape.admin.entity.AdminAiChatSession",
            "com.grape.admin.entity.AdminAiChatMessage",
    };

    @Bean
    @Primary
    public LocalContainerEntityManagerFactoryBean adminEntityManagerFactory(
            @Qualifier("adminDataSource") DataSource dataSource,
            JpaProperties jpaProperties) {
        LocalContainerEntityManagerFactoryBean emf = new LocalContainerEntityManagerFactoryBean();
        emf.setPersistenceUnitName("admin");
        emf.setDataSource(dataSource);
        emf.setJpaVendorAdapter(new HibernateJpaVendorAdapter());

        // 精确实体注册（排除 entity.readonly 子包，P4）
        PersistenceUnitManager pum = new PersistenceUnitManager() {
            private final MutablePersistenceUnitInfo pui = buildPui();

            private MutablePersistenceUnitInfo buildPui() {
                MutablePersistenceUnitInfo info = new MutablePersistenceUnitInfo() {
                    @Override
                    public ClassLoader getNewTempClassLoader() {
                        // MutablePersistenceUnitInfo 默认抛 UnsupportedOperationException，
                        // Hibernate 构建 SessionFactory 时需要 temp classloader
                        return getClass().getClassLoader();
                    }
                };
                info.setPersistenceUnitName("admin");
                info.setPersistenceProviderClassName(HibernatePersistenceProvider.class.getName());
                info.setNonJtaDataSource(dataSource);
                info.setExcludeUnlistedClasses(true);
                for (String cls : ADMIN_ENTITY_CLASSES) {
                    info.addManagedClassName(cls);
                }
                return info;
            }

            @Override
            public PersistenceUnitInfo obtainDefaultPersistenceUnitInfo() {
                return pui;
            }

            @Override
            public PersistenceUnitInfo obtainPersistenceUnitInfo(String persistenceUnitName) {
                return pui;
            }
        };
        emf.setPersistenceUnitManager(pum);

        Map<String, Object> props = new HashMap<>(jpaProperties.getProperties());
        props.putIfAbsent("hibernate.dialect", "org.hibernate.dialect.MySQL8Dialect");
        emf.setJpaPropertyMap(props);
        return emf;
    }
}
