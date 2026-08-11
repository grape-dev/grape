package com.grape.admin.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * 拦截器注册：/api/admin/** 除登录/健康检查外全部走 JWT 认证 + RBAC。
 */
@Configuration
public class WebConfig implements WebMvcConfigurer {

    private final JwtAuthInterceptor jwtAuthInterceptor;
    private final RbacHandlerInterceptor rbacHandlerInterceptor;

    public WebConfig(JwtAuthInterceptor jwtAuthInterceptor, RbacHandlerInterceptor rbacHandlerInterceptor) {
        this.jwtAuthInterceptor = jwtAuthInterceptor;
        this.rbacHandlerInterceptor = rbacHandlerInterceptor;
    }

    @Override
    public void addInterceptors(InterceptorRegistry registry) {
        registry.addInterceptor(jwtAuthInterceptor)
                .addPathPatterns("/api/admin/**")
                .excludePathPatterns("/api/admin/auth/**", "/api/admin/health");
        registry.addInterceptor(rbacHandlerInterceptor)
                .addPathPatterns("/api/admin/**")
                .excludePathPatterns("/api/admin/auth/**", "/api/admin/health");
    }
}
