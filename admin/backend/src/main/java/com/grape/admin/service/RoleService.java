package com.grape.admin.service;

import com.grape.admin.config.AuthContext;
import com.grape.admin.config.RbacPermissionLoader;
import com.grape.admin.dto.CreateRoleRequest;
import com.grape.admin.dto.RoleDto;
import com.grape.admin.entity.AdminRole;
import com.grape.admin.entity.AdminRoleMenuRel;
import com.grape.admin.repository.AdminRoleMenuRelRepository;
import com.grape.admin.repository.AdminRoleRepository;
import com.grape.admin.repository.AdminUserRoleRelRepository;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.servlet.http.HttpServletRequest;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

@Service
public class RoleService {

    private static final Set<String> RESERVED_CODES = Set.of("SUPER_ADMIN", "VIEWER", "OPS");

    private final AdminRoleRepository roleRepository;
    private final AdminRoleMenuRelRepository roleMenuRelRepository;
    private final AdminUserRoleRelRepository userRoleRelRepository;
    private final RbacPermissionLoader permissionLoader;

    public RoleService(AdminRoleRepository roleRepository,
                       AdminRoleMenuRelRepository roleMenuRelRepository,
                       AdminUserRoleRelRepository userRoleRelRepository,
                       RbacPermissionLoader permissionLoader) {
        this.roleRepository = roleRepository;
        this.roleMenuRelRepository = roleMenuRelRepository;
        this.userRoleRelRepository = userRoleRelRepository;
        this.permissionLoader = permissionLoader;
    }

    public List<RoleDto> listRoles() {
        return roleRepository.findAllByOrderByIdAsc().stream().map(this::toDto).collect(Collectors.toList());
    }

    @Transactional
    public RoleDto createRole(CreateRoleRequest req, HttpServletRequest request) {
        String operator = AuthContext.username(request);
        String code = req.getCode().trim().toUpperCase();
        if (RESERVED_CODES.contains(code)) {
            throw new BizException("角色 code 使用了保留字: " + code);
        }
        if (roleRepository.findByCode(code).isPresent()) {
            throw new BizException("角色 code 已存在: " + code);
        }
        AdminRole role = new AdminRole();
        role.setCode(code);
        role.setName(req.getName());
        role.setIsProtected(0);
        role.setCreatedBy(operator);
        role.setUpdatedBy(operator);
        roleRepository.save(role);
        return toDto(role);
    }

    @Transactional
    public void deleteRole(Long roleId, HttpServletRequest request) {
        String operator = AuthContext.username(request);
        AdminRole role = requireRole(roleId);
        if (role.getIsProtected() != null && role.getIsProtected() == 1) {
            throw new BizException("保护角色不允许删除");
        }
        if (userRoleRelRepository.existsByRoleId(roleId)) {
            throw new BizException("角色已绑定用户，请先解绑");
        }
        role.setDeleted(1);
        role.setUpdatedBy(operator);
        roleRepository.save(role);
        roleMenuRelRepository.softDeleteByRoleId(roleId, operator);
    }

    public List<Long> getPermissions(Long roleId) {
        requireRole(roleId);
        return roleMenuRelRepository.findByRoleId(roleId).stream()
                .map(AdminRoleMenuRel::getMenuId).collect(Collectors.toList());
    }

    @Transactional
    public void savePermissions(Long roleId, List<Long> menuIds, HttpServletRequest request) {
        String operator = AuthContext.username(request);
        AdminRole role = requireRole(roleId);
        if (role.getIsProtected() != null && role.getIsProtected() == 1) {
            throw new BizException("保护角色的权限不允许修改");
        }
        if (menuIds == null || menuIds.isEmpty()) {
            throw new BizException("至少勾选一个菜单");
        }
        roleMenuRelRepository.softDeleteByRoleId(roleId, operator);
        for (Long menuId : menuIds) {
            AdminRoleMenuRel rel = new AdminRoleMenuRel();
            rel.setRoleId(roleId);
            rel.setMenuId(menuId);
            rel.setCreatedBy(operator);
            rel.setUpdatedBy(operator);
            roleMenuRelRepository.save(rel);
        }
        permissionLoader.invalidate();
    }

    private RoleDto toDto(AdminRole role) {
        RoleDto dto = new RoleDto();
        dto.setId(role.getId());
        dto.setCode(role.getCode());
        dto.setName(role.getName());
        dto.setIsProtected(role.getIsProtected());
        dto.setMenuIds(roleMenuRelRepository.findByRoleId(role.getId()).stream()
                .map(AdminRoleMenuRel::getMenuId).collect(Collectors.toList()));
        return dto;
    }

    private AdminRole requireRole(Long roleId) {
        return roleRepository.findById(roleId)
                .orElseThrow(() -> new BizException("角色不存在: " + roleId));
    }

    public static class BizException extends RuntimeException {
        public BizException(String message) {
            super(message);
        }
    }
}
