package com.grape.admin.config;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.autoconfigure.orm.jpa.JpaProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;
import org.springframework.orm.jpa.LocalContainerEntityManagerFactoryBean;
import org.springframework.orm.jpa.persistenceunit.DefaultPersistenceUnitManager;
import org.springframework.orm.jpa.vendor.HibernateJpaVendorAdapter;

import javax.sql.DataSource;
import java.util.HashMap;
import java.util.Map;

/**
 * grape 持久化单元（grape 业务只读实体）：扫描 entity.readonly + repository.readonly。
 *
 * 承载 grape 业务表（agent_runtimes/agent_tasks/agent_type/managed_repos/runtime_credentials/skill）的
 * 读写（含 SkillRoRepository 的 nativeQuery 写操作，事务走 grapeTransactionManager，P13）。
 */
@Configuration
@EnableJpaRepositories(
        basePackages = "com.grape.admin.repository.readonly",
        entityManagerFactoryRef = "grapeEntityManagerFactory",
        transactionManagerRef = "grapeTransactionManager")
public class GrapeJpaConfig {

    @Bean
    public LocalContainerEntityManagerFactoryBean grapeEntityManagerFactory(
            @Qualifier("grapeDataSource") DataSource dataSource,
            JpaProperties jpaProperties) {
        LocalContainerEntityManagerFactoryBean emf = new LocalContainerEntityManagerFactoryBean();
        emf.setPersistenceUnitName("grape");
        emf.setDataSource(dataSource);
        emf.setJpaVendorAdapter(new HibernateJpaVendorAdapter());

        DefaultPersistenceUnitManager pum = new DefaultPersistenceUnitManager();
        pum.setDefaultPersistenceUnitName("grape");
        pum.setDefaultDataSource(dataSource);
        pum.setPackagesToScan("com.grape.admin.entity.readonly");
        pum.afterPropertiesSet();
        emf.setPersistenceUnitManager(pum);

        Map<String, Object> props = new HashMap<>(jpaProperties.getProperties());
        props.putIfAbsent("hibernate.dialect", "org.hibernate.dialect.MySQL8Dialect");
        emf.setJpaPropertyMap(props);
        return emf;
    }
}
