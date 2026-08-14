package com.grape.admin.service;

import com.grape.admin.config.JwtUtil;
import com.grape.admin.config.TokenBlacklist;
import com.grape.admin.dto.LoginResponse;
import com.grape.admin.entity.AdminUser;
import com.grape.admin.repository.AdminRoleRepository;
import com.grape.admin.repository.AdminUserRepository;
import com.grape.admin.repository.AdminUserRoleRelRepository;
import io.jsonwebtoken.Claims;
import io.jsonwebtoken.JwtException;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;

/**
 * admin 独立账号认证：用户名+密码（BCrypt），JWT access(2h) + refresh(7d)。
 */
@Service
public class AuthService {

    private static final long REFRESH_EXPIRE_MINUTES = 7 * 24 * 60;

    private final AdminUserRepository userRepository;
    private final JwtUtil jwtUtil;
    private final TokenBlacklist blacklist;
    private final AdminUserRoleRelRepository roleRelRepository;
    private final AdminRoleRepository roleRepository;
    private final BCryptPasswordEncoder passwordEncoder = new BCryptPasswordEncoder();

    public AuthService(AdminUserRepository userRepository, JwtUtil jwtUtil, TokenBlacklist blacklist,
                       AdminUserRoleRelRepository roleRelRepository, AdminRoleRepository roleRepository) {
        this.userRepository = userRepository;
        this.jwtUtil = jwtUtil;
        this.blacklist = blacklist;
        this.roleRelRepository = roleRelRepository;
        this.roleRepository = roleRepository;
    }

    public LoginResponse login(String username, String password) {
        AdminUser user = userRepository.findByUsername(username)
                .orElseThrow(() -> new AuthException("用户名或密码错误"));
        if (user.getStatus() == null || user.getStatus() != 1) {
            throw new AuthException("用户已被禁用");
        }
        if (!passwordEncoder.matches(password, user.getPasswordHash())) {
            throw new AuthException("用户名或密码错误");
        }
        return buildResponse(user);
    }

    public LoginResponse refresh(String refreshToken) {
        if (blacklist.isRefreshDenied(refreshToken)) {
            throw new AuthException("refresh token 已失效");
        }
        Claims claims;
        try {
            claims = jwtUtil.parse(refreshToken);
        } catch (JwtException | IllegalArgumentException e) {
            throw new AuthException("refresh token 无效或已过期");
        }
        if (!"refresh".equals(claims.get("type", String.class))) {
            throw new AuthException("token 类型错误");
        }
        Long userId = Long.valueOf(claims.getSubject());
        AdminUser user = userRepository.findById(userId)
                .orElseThrow(() -> new AuthException("用户不存在"));
        if (user.getStatus() == null || user.getStatus() != 1) {
            throw new AuthException("用户已被禁用");
        }
        return buildResponse(user);
    }

    public void logout(String refreshToken) {
        blacklist.denyRefresh(refreshToken);
    }

    private LoginResponse buildResponse(AdminUser user) {
        LoginResponse resp = new LoginResponse();
        resp.setUserId(user.getId());
        resp.setUsername(user.getUsername());
        resp.setNickname(user.getNickname());
        resp.setRoles(roleRelRepository.findByUserIdAndDeleted(user.getId(), 0).stream()
                .map(rel -> roleRepository.findById(rel.getRoleId()))
                .filter(java.util.Optional::isPresent)
                .map(r -> r.get().getName())
                .collect(java.util.stream.Collectors.toList()));
        resp.setToken(jwtUtil.generateToken(user.getId(), user.getUsername()));
        resp.setRefreshToken(jwtUtil.generateToken(user.getId(), user.getUsername(), "refresh", REFRESH_EXPIRE_MINUTES));
        return resp;
    }

    /** 认证失败统一异常 → 401 */
    public static class AuthException extends RuntimeException {
        public AuthException(String message) {
            super(message);
        }
    }
}
