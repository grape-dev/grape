package com.grape.admin.service;

import com.grape.admin.config.AuthContext;
import com.grape.admin.dto.CreateUserRequest;
import com.grape.admin.dto.UserDto;
import com.grape.admin.entity.AdminRole;
import com.grape.admin.entity.AdminUser;
import com.grape.admin.entity.AdminUserRoleRel;
import com.grape.admin.repository.AdminRoleRepository;
import com.grape.admin.repository.AdminUserRepository;
import com.grape.admin.repository.AdminUserRoleRelRepository;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.PageRequest;
import org.springframework.data.domain.Sort;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import javax.servlet.http.HttpServletRequest;
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

@Service
public class UserService {

    private static final BCryptPasswordEncoder PASSWORD_ENCODER = new BCryptPasswordEncoder();

    private final AdminUserRepository userRepository;
    private final AdminRoleRepository roleRepository;
    private final AdminUserRoleRelRepository userRoleRelRepository;

    public UserService(AdminUserRepository userRepository,
                       AdminRoleRepository roleRepository,
                       AdminUserRoleRelRepository userRoleRelRepository) {
        this.userRepository = userRepository;
        this.roleRepository = roleRepository;
        this.userRoleRelRepository = userRoleRelRepository;
    }

    public Page<UserDto> listUsers(String keyword, int page, int size) {
        Page<AdminUser> users;
        if (keyword == null || keyword.isBlank()) {
            users = userRepository.findAll(PageRequest.of(page, size, Sort.by(Sort.Direction.DESC, "id")));
        } else {
            users = userRepository.findAllByUsernameContaining(keyword,
                    PageRequest.of(page, size, Sort.by(Sort.Direction.DESC, "id")));
        }
        Map<Long, List<AdminRole>> rolesByUser = loadRolesByUser(users.getContent());
        return users.map(u -> toDto(u, rolesByUser.getOrDefault(u.getId(), List.of())));
    }

    @Transactional
    public UserDto createUser(CreateUserRequest req, HttpServletRequest request) {
        String operator = AuthContext.username(request);
        if (userRepository.findByUsername(req.getUsername()).isPresent()) {
            throw new BizException("用户名已存在");
        }
        AdminUser user = new AdminUser();
        user.setUsername(req.getUsername());
        user.setNickname(req.getNickname() == null ? "" : req.getNickname());
        user.setPasswordHash(PASSWORD_ENCODER.encode(req.getPassword()));
        user.setStatus(1);
        user.setCreatedBy(operator);
        user.setUpdatedBy(operator);
        userRepository.save(user);
        return toDto(user, List.of());
    }

    @Transactional
    public void updateStatus(Long userId, Integer status, HttpServletRequest request) {
        String operator = AuthContext.username(request);
        AdminUser user = requireUser(userId);
        if (status != 0 && status != 1) {
            throw new BizException("status 仅支持 0(禁用)/1(启用)");
        }
        boolean isSuperAdmin = userRoleRelRepository.findByUserId(userId).stream()
                .map(r -> roleRepository.findById(r.getRoleId()).orElse(null))
                .anyMatch(r -> r != null && "SUPER_ADMIN".equals(r.getCode()));
        if (isSuperAdmin && status == 0) {
            throw new BizException("不允许禁用 SUPER_ADMIN 用户");
        }
        user.setStatus(status);
        user.setUpdatedBy(operator);
        userRepository.save(user);
    }

    @Transactional
    public void assignRoles(Long userId, List<Long> roleIds, HttpServletRequest request) {
        String operator = AuthContext.username(request);
        AdminUser user = requireUser(userId);
        if (roleIds == null || roleIds.isEmpty()) {
            throw new BizException("至少绑定一个角色");
        }
        for (Long rid : roleIds) {
            if (!roleRepository.findById(rid).isPresent()) {
                throw new BizException("角色不存在: " + rid);
            }
        }
        if (roleIds.contains(roleRepository.findByCode("SUPER_ADMIN").map(AdminRole::getId).orElse(-1L))) {
            // 保护：SUPER_ADMIN 只允许绑定给启用用户（下面校验）
        }
        if (user.getStatus() != 1 && roleIds.stream().anyMatch(id ->
                roleRepository.findById(id).map(r -> "SUPER_ADMIN".equals(r.getCode())).orElse(false))) {
            throw new BizException("不允许给禁用用户绑定 SUPER_ADMIN 角色");
        }
        userRoleRelRepository.softDeleteByUserId(userId, operator);
        for (Long rid : roleIds) {
            AdminUserRoleRel rel = new AdminUserRoleRel();
            rel.setUserId(userId);
            rel.setRoleId(rid);
            rel.setCreatedBy(operator);
            rel.setUpdatedBy(operator);
            userRoleRelRepository.save(rel);
        }
    }

    /** 编辑用户信息（当前仅昵称） */
    @Transactional
    public UserDto updateUser(Long userId, String nickname, HttpServletRequest request) {
        String operator = AuthContext.username(request);
        AdminUser user = requireUser(userId);
        if (nickname != null) {
            user.setNickname(nickname);
        }
        user.setUpdatedBy(operator);
        userRepository.save(user);
        List<AdminRole> roles = loadRolesByUser(List.of(user)).getOrDefault(user.getId(), List.of());
        return toDto(user, roles);
    }

    /** 重置密码：生成随机 12 位密码，返回明文一次性展示 */
    @Transactional
    public String resetPassword(Long userId, HttpServletRequest request) {
        String operator = AuthContext.username(request);
        AdminUser user = requireUser(userId);
        String newPwd = java.util.UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        user.setPasswordHash(PASSWORD_ENCODER.encode(newPwd));
        user.setUpdatedBy(operator);
        userRepository.save(user);
        return newPwd;
    }

    /** 角色分布统计（全量有效绑定） */
    public List<Map<String, Object>> roleStats() {
        Map<Long, AdminRole> rolesById = roleRepository.findAll().stream()
                .collect(Collectors.toMap(AdminRole::getId, Function.identity()));
        List<Map<String, Object>> stats = userRoleRelRepository.countActiveByRoleId().stream()
                .map(row -> {
                    Map<String, Object> m = new java.util.LinkedHashMap<>();
                    Long roleId = (Long) row[0];
                    AdminRole role = rolesById.get(roleId);
                    m.put("roleId", roleId);
                    m.put("name", role == null ? String.valueOf(roleId) : role.getName());
                    m.put("code", role == null ? "" : role.getCode());
                    m.put("count", ((Number) row[1]).intValue());
                    return m;
                })
                .collect(Collectors.toList());
        stats.sort(java.util.Comparator.<Map<String, Object>>comparingInt(a -> ((Number) a.get("count")).intValue()).reversed());
        return stats;
    }

    private Map<Long, List<AdminRole>> loadRolesByUser(List<AdminUser> users) {
        List<Long> userIds = users.stream().map(AdminUser::getId).collect(Collectors.toList());
        if (userIds.isEmpty()) {
            return Map.of();
        }
        List<AdminUserRoleRel> rels = userRoleRelRepository.findByUserIdIn(userIds);
        Map<Long, AdminRole> rolesById = roleRepository.findAll().stream()
                .collect(Collectors.toMap(AdminRole::getId, Function.identity()));
        return rels.stream().collect(Collectors.groupingBy(AdminUserRoleRel::getUserId,
                Collectors.mapping(r -> rolesById.get(r.getRoleId()), Collectors.toList())));
    }

    private UserDto toDto(AdminUser u, List<AdminRole> roles) {
        UserDto dto = new UserDto();
        dto.setId(u.getId());
        dto.setUsername(u.getUsername());
        dto.setNickname(u.getNickname());
        dto.setStatus(u.getStatus());
        dto.setCreatedAt(u.getCreatedAt());
        dto.setCreatedBy(u.getCreatedBy());
        dto.setUpdatedBy(u.getUpdatedBy());
        dto.setRoleIds(roles.stream().map(AdminRole::getId).collect(Collectors.toList()));
        dto.setRoleNames(roles.stream().map(AdminRole::getName).collect(Collectors.toList()));
        return dto;
    }

    private AdminUser requireUser(Long userId) {
        return userRepository.findById(userId)
                .orElseThrow(() -> new BizException("用户不存在: " + userId));
    }

    public static class BizException extends RuntimeException {
        public BizException(String message) {
            super(message);
        }
    }
}
