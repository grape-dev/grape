package com.grape.admin.config;

import com.grape.admin.entity.AdminRole;
import com.grape.admin.entity.AdminUserRoleRel;
import com.grape.admin.repository.AdminRoleRepository;
import com.grape.admin.repository.AdminUserRoleRelRepository;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.HandlerInterceptor;

import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * RBAC 拦截器（完整版）：
 * - 菜单 code → API 前缀映射（菜单权限控制到接口）
 * - SUPER_ADMIN 全放行；VIEWER（只读角色）仅允许 GET
 * - 其余角色按「角色→菜单→API 前缀」匹配，无权限 403
 */
@Component
public class RbacHandlerInterceptor implements HandlerInterceptor {

    /** 菜单 code → API 前缀（新菜单需在此登记；/api/admin/menus 对已认证用户恒放行） */
    private static final Map<String, List<String>> MENU_API_PREFIXES = Map.of(
            "runtime.devices", List.of("/api/admin/devices", "/api/admin/tasks"),
            "runtime.engines", List.of("/api/admin/engines"),
            "runtime.credentials", List.of("/api/admin/credentials"),
            "ai.assistant", List.of("/api/admin/ai/chat", "/api/admin/ai/templates"),
            "ai.history", List.of("/api/admin/ai/history"),
            "skill", List.of("/api/admin/skills"),
            "managed-repos", List.of("/api/admin/repos"),
            "template-repos", List.of("/api/admin/templates"),
            "sys.users", List.of("/api/admin/users"),
            "sys.roles", List.of("/api/admin/roles")
    );

    private final AdminUserRoleRelRepository userRoleRelRepository;
    private final AdminRoleRepository roleRepository;
    private final RbacPermissionLoader permissionLoader;

    public RbacHandlerInterceptor(AdminUserRoleRelRepository userRoleRelRepository,
                                  AdminRoleRepository roleRepository,
                                  RbacPermissionLoader permissionLoader) {
        this.userRoleRelRepository = userRoleRelRepository;
        this.roleRepository = roleRepository;
        this.permissionLoader = permissionLoader;
    }

    @Override
    public boolean preHandle(HttpServletRequest request, HttpServletResponse response, Object handler) throws Exception {
        Long userId = (Long) request.getAttribute(JwtAuthInterceptor.ATTR_USER_ID);
        if (userId == null) {
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            return false;
        }
        List<AdminRole> roles = userRoleRelRepository.findByUserId(userId).stream()
                .map(AdminUserRoleRel::getRoleId)
                .map(roleRepository::findById)
                .filter(java.util.Optional::isPresent)
                .map(java.util.Optional::get)
                .collect(Collectors.toList());
        if (roles.isEmpty()) {
            return forbidden(response, "用户未绑定任何角色");
        }
        boolean superAdmin = roles.stream().anyMatch(r -> "SUPER_ADMIN".equals(r.getCode()));
        if (superAdmin) {
            return true;
        }
        String path = request.getRequestURI();
        if ("/api/admin/menus".equals(path)) {
            return true;   // 登录后菜单树本身可见（只读，不含写能力）
        }
        // VIEWER（只读角色）仅允许 GET
        boolean readOnly = roles.stream().allMatch(r -> "VIEWER".equals(r.getCode()));
        if (readOnly && !"GET".equalsIgnoreCase(request.getMethod())) {
            return forbidden(response, "只读角色不允许写操作");
        }
        // 角色 → 菜单 code → API 前缀匹配
        Set<String> allowedCodes = roles.stream()
                .flatMap(r -> permissionLoader.menuCodesForRole(r.getId()).stream())
                .collect(Collectors.toSet());
        boolean matched = MENU_API_PREFIXES.entrySet().stream()
                .filter(e -> allowedCodes.contains(e.getKey()))
                .flatMap(e -> e.getValue().stream())
                .anyMatch(path::startsWith);
        if (!matched) {
            return forbidden(response, "无该菜单权限");
        }
        return true;
    }

    private boolean forbidden(HttpServletResponse response, String msg) throws Exception {
        response.setStatus(HttpServletResponse.SC_FORBIDDEN);
        response.setContentType("application/json;charset=UTF-8");
        response.getWriter().write("{\"code\":403,\"message\":\"" + msg + "\"}");
        return false;
    }
}
