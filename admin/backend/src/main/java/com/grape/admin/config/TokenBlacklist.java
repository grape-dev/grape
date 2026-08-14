package com.grape.admin.config;

import org.springframework.stereotype.Component;

import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 登出黑名单（内存）：refresh token 登出后立即失效；access token 短时自过期。
 */
@Component
public class TokenBlacklist {

    private final Set<String> deniedRefreshTokens = ConcurrentHashMap.newKeySet();

    public void denyRefresh(String token) {
        if (token != null && !token.isBlank()) {
            deniedRefreshTokens.add(token);
        }
    }

    public boolean isRefreshDenied(String token) {
        return token != null && deniedRefreshTokens.contains(token);
    }
}
