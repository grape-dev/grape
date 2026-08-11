package com.grape.admin.config;

import com.grape.admin.entity.AdminUser;
import com.grape.admin.repository.AdminUserRepository;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.HandlerInterceptor;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/**
 * RBAC 拦截器。
 * P0 阶段：仅校验用户仍存在且启用（占位，放行一切已认证请求）；
 * P1.3 补全：路径 → 菜单权限匹配，SUPER_ADMIN 全放行，VIEWER 仅 GET，无权限 403。
 */
@Component
public class RbacHandlerInterceptor implements HandlerInterceptor {

    private final AdminUserRepository userRepository;

    public RbacHandlerInterceptor(AdminUserRepository userRepository) {
        this.userRepository = userRepository;
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws Exception {
        Long userId = (Long) request.getAttribute(JwtAuthInterceptor.ATTR_USER_ID);
        if (userId == null) {
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            return false;
        }
        AdminUser user = userRepository.findById(userId).orElse(null);
        if (user == null || user.getStatus() == null || user.getStatus() != 1) {
            response.setStatus(HttpServletResponse.SC_FORBIDDEN);
            response.setContentType("application/json;charset=UTF-8");
            response.getWriter().write("{\"code\":403,\"message\":\"用户不存在或已禁用\"}");
            return false;
        }
        // TODO(P1.3): 菜单权限匹配
        return true;
    }
}
