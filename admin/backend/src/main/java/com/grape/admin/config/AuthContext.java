package com.grape.admin.config;

import javax.servlet.http.HttpServletRequest;

/**
 * 当前登录用户上下文（从 JWT 拦截器写入的 request attribute 读取）。
 */
public final class AuthContext {

    private AuthContext() {
    }

    public static Long userId(HttpServletRequest request) {
        return (Long) request.getAttribute(JwtAuthInterceptor.ATTR_USER_ID);
    }

    public static String username(HttpServletRequest request) {
        return (String) request.getAttribute(JwtAuthInterceptor.ATTR_USERNAME);
    }
}
