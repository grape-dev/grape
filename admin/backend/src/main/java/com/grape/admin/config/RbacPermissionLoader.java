package com.grape.admin.config;

import com.grape.admin.entity.AdminPermissionMenu;
import com.grape.admin.entity.AdminRoleMenuRel;
import com.grape.admin.repository.AdminPermissionMenuRepository;
import com.grape.admin.repository.AdminRoleMenuRelRepository;
import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;

/**
 * 角色→菜单 code 映射加载器（60s 缓存；角色权限变更后最多 60s 生效）。
 */
@Component
public class RbacPermissionLoader {

    private static final long TTL_MS = 60_000;

    private final AdminRoleMenuRelRepository roleMenuRelRepository;
    private final AdminPermissionMenuRepository menuRepository;

    private volatile Map<Long, Set<String>> roleMenuCodes = Map.of();
    private volatile long loadedAt = 0;

    public RbacPermissionLoader(AdminRoleMenuRelRepository roleMenuRelRepository,
                                AdminPermissionMenuRepository menuRepository) {
        this.roleMenuRelRepository = roleMenuRelRepository;
        this.menuRepository = menuRepository;
    }

    public Set<String> menuCodesForRole(Long roleId) {
        refreshIfStale();
        return roleMenuCodes.getOrDefault(roleId, Set.of());
    }

    /** 权限变更后调用：立即失效缓存，下次请求重新加载（保证"改权限立即生效"） */
    public void invalidate() {
        loadedAt = 0;
    }

    private synchronized void refreshIfStale() {
        long now = System.currentTimeMillis();
        if (now - loadedAt < TTL_MS) {
            return;
        }
        Map<Long, AdminPermissionMenu> menuById = menuRepository.findAllByOrderBySortAsc().stream()
                .collect(Collectors.toMap(AdminPermissionMenu::getId, m -> m));
        roleMenuCodes = roleMenuRelRepository.findAll().stream()
                .collect(Collectors.groupingBy(AdminRoleMenuRel::getRoleId,
                        Collectors.mapping(r -> {
                            AdminPermissionMenu m = menuById.get(r.getMenuId());
                            return m == null ? "" : m.getCode();
                        }, Collectors.toSet())));
        loadedAt = now;
    }
}
