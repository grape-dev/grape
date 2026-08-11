package com.grape.admin.config;

import com.grape.admin.entity.AdminRole;
import com.grape.admin.entity.AdminUser;
import com.grape.admin.entity.AdminUserRoleRel;
import com.grape.admin.repository.AdminRoleRepository;
import com.grape.admin.repository.AdminUserRepository;
import com.grape.admin.repository.AdminUserRoleRelRepository;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/**
 * 种子管理员：启动时若不存在 admin 用户，则用 ADMIN_SEED_PASSWORD 创建并绑定 SUPER_ADMIN。
 * 密码仅从环境变量注入，不入库明文、不入 git。
 */
@Component
public class AdminSeedInitializer implements ApplicationRunner {

    private static final Logger log = LoggerFactory.getLogger(AdminSeedInitializer.class);

    @Value("${ADMIN_SEED_PASSWORD:}")
    private String seedPassword;

    private final AdminUserRepository userRepository;
    private final AdminRoleRepository roleRepository;
    private final AdminUserRoleRelRepository userRoleRelRepository;

    public AdminSeedInitializer(AdminUserRepository userRepository,
                                AdminRoleRepository roleRepository,
                                AdminUserRoleRelRepository userRoleRelRepository) {
        this.userRepository = userRepository;
        this.roleRepository = roleRepository;
        this.userRoleRelRepository = userRoleRelRepository;
    }

    @Override
    @Transactional
    public void run(ApplicationArguments args) {
        if (userRepository.findByUsername("admin").isPresent()) {
            return;
        }
        if (seedPassword == null || seedPassword.length() < 8) {
            throw new IllegalStateException("ADMIN_SEED_PASSWORD 未配置（至少 8 位），首次启动无法创建种子管理员");
        }
        AdminUser admin = new AdminUser();
        admin.setUsername("admin");
        admin.setNickname("超级管理员");
        admin.setPasswordHash(new BCryptPasswordEncoder().encode(seedPassword));
        admin.setStatus(1);
        admin.setCreatedBy("system");
        admin.setUpdatedBy("system");
        userRepository.save(admin);

        AdminRole superRole = roleRepository.findByCode("SUPER_ADMIN")
                .orElseThrow(() -> new IllegalStateException("SUPER_ADMIN 角色缺失（V1 迁移未执行？）"));
        AdminUserRoleRel rel = new AdminUserRoleRel();
        rel.setUserId(admin.getId());
        rel.setRoleId(superRole.getId());
        rel.setCreatedBy("system");
        rel.setUpdatedBy("system");
        userRoleRelRepository.save(rel);
        log.info("种子管理员 admin 已创建并绑定 SUPER_ADMIN");
    }
}
