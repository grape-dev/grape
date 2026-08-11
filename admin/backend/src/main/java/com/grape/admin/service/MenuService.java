package com.grape.admin.service;

import com.grape.admin.entity.AdminPermissionMenu;
import com.grape.admin.entity.AdminRole;
import com.grape.admin.entity.AdminRoleMenuRel;
import com.grape.admin.entity.AdminUserRoleRel;
import com.grape.admin.repository.AdminPermissionMenuRepository;
import com.grape.admin.repository.AdminRoleMenuRelRepository;
import com.grape.admin.repository.AdminRoleRepository;
import com.grape.admin.repository.AdminUserRoleRelRepository;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Collectors;

/**
 * 菜单权限：当前用户可见菜单树（SUPER_ADMIN 全量，其余按角色绑定）。
 */
@Service
public class MenuService {

    private final AdminUserRoleRelRepository userRoleRelRepository;
    private final AdminRoleRepository roleRepository;
    private final AdminRoleMenuRelRepository roleMenuRelRepository;
    private final AdminPermissionMenuRepository menuRepository;

    public MenuService(AdminUserRoleRelRepository userRoleRelRepository,
                       AdminRoleRepository roleRepository,
                       AdminRoleMenuRelRepository roleMenuRelRepository,
                       AdminPermissionMenuRepository menuRepository) {
        this.userRoleRelRepository = userRoleRelRepository;
        this.roleRepository = roleRepository;
        this.roleMenuRelRepository = roleMenuRelRepository;
        this.menuRepository = menuRepository;
    }

    public List<Map<String, Object>> menuTreeForUser(Long userId) {
        List<AdminPermissionMenu> all = menuRepository.findAllByOrderBySortAsc();
        Set<Long> visibleMenuIds;
        List<AdminUserRoleRel> rels = userRoleRelRepository.findByUserId(userId);
        boolean superAdmin = rels.stream()
                .map(r -> roleRepository.findById(r.getRoleId()).orElse(null))
                .filter(r -> r != null && "SUPER_ADMIN".equals(r.getCode()))
                .findAny().isPresent();
        if (superAdmin) {
            visibleMenuIds = all.stream().map(AdminPermissionMenu::getId).collect(Collectors.toSet());
        } else {
            Set<Long> bound = rels.stream()
                    .flatMap(r -> roleMenuRelRepository.findByRoleId(r.getRoleId()).stream())
                    .map(AdminRoleMenuRel::getMenuId)
                    .collect(Collectors.toSet());
            // 子菜单可见时自动带上父菜单，避免出现"有叶子没树干"的空树
            Set<Long> withParents = new java.util.HashSet<>(bound);
            all.stream()
                    .filter(m -> bound.contains(m.getId()) && m.getParentId() != null && m.getParentId() > 0)
                    .forEach(m -> withParents.add(m.getParentId()));
            visibleMenuIds = withParents;
        }
        List<AdminPermissionMenu> visible = all.stream()
                .filter(m -> visibleMenuIds.contains(m.getId())).collect(Collectors.toList());

        List<Map<String, Object>> tree = new ArrayList<>();
        for (AdminPermissionMenu parent : visible.stream()
                .filter(m -> m.getParentId() == null || m.getParentId() == 0)
                .sorted(Comparator.comparing(AdminPermissionMenu::getSort))
                .collect(Collectors.toList())) {
            Map<String, Object> node = toNode(parent);
            List<Map<String, Object>> children = visible.stream()
                    .filter(m -> parent.getId().equals(m.getParentId()))
                    .sorted(Comparator.comparing(AdminPermissionMenu::getSort))
                    .map(this::toNode)
                    .collect(Collectors.toList());
            node.put("children", children);
            tree.add(node);
        }
        return tree;
    }

    private Map<String, Object> toNode(AdminPermissionMenu m) {
        Map<String, Object> node = new LinkedHashMap<>();
        node.put("id", m.getId());
        node.put("code", m.getCode());
        node.put("name", m.getName());
        node.put("path", m.getPath());
        return node;
    }
}
