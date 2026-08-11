package com.grape.admin.config;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.HandlerInterceptor;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/**
 * JWT 认证拦截器：解析 Authorization: Bearer <token>，成功则写入
 * request attribute（userId/username），失败返回 401。
 */
@Component
public class JwtAuthInterceptor implements HandlerInterceptor {

    public static final String ATTR_USER_ID = "adminUserId";
    public static final String ATTR_USERNAME = "adminUsername";

    private final JwtUtil jwtUtil;

    public JwtAuthInterceptor(JwtUtil jwtUtil) {
        this.jwtUtil = jwtUtil;
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws Exception {
        if ("OPTIONS".equalsIgnoreCase(request.getMethod())) {
            return true;
        }
        String auth = request.getHeader("Authorization");
        if (auth == null || !auth.startsWith("Bearer ")) {
            return unauthorized(response, "缺少 Authorization 头");
        }
        try {
            Claims claims = jwtUtil.parse(auth.substring(7));
            request.setAttribute(ATTR_USER_ID, Long.valueOf(claims.getSubject()));
            request.setAttribute(ATTR_USERNAME, claims.get("username", String.class));
            return true;
        } catch (JwtException | IllegalArgumentException e) {
            return unauthorized(response, "token 无效或已过期");
        }
    }

    private boolean unauthorized(HttpServletResponse response, String msg) throws Exception {
        response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
        response.setContentType("application/json;charset=UTF-8");
        response.getWriter().write("{\"code\":401,\"message\":\"" + msg + "\"}");
        return false;
    }
}
