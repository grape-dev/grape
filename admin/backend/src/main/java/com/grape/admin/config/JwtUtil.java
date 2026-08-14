package com.grape.admin.config;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.SignatureAlgorithm;
import io.jsonwebtoken.security.Keys;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.annotation.PostConstruct;
import java.nio.charset.StandardCharsets;
import java.security.Key;
import java.util.Date;

/**
 * JWT 工具（admin 独立账号体系）：HS256 签发/解析。
 * secret 必须配置（ADMIN_JWT_SECRET），为空则启动失败。
 */
@Component
public class JwtUtil {

    @Value("${app.jwt-secret:}")
    String secret;

    @Value("${app.jwt-expire-minutes:120}")
    long expireMinutes;

    private Key key;

    @PostConstruct
    void init() {
        if (secret == null || secret.isBlank() || secret.length() < 32) {
            throw new IllegalStateException("ADMIN_JWT_SECRET 未配置或长度不足 32 字符，禁止启动");
        }
        this.key = Keys.hmacShaKeyFor(secret.getBytes(StandardCharsets.UTF_8));
    }

    public String generateToken(Long userId, String username) {
        return generateToken(userId, username, null, expireMinutes);
    }

    /** 带 type（access/refresh）与自定义有效期的签发 */
    public String generateToken(Long userId, String username, String type, long expireMinutes) {
        Date now = new Date();
        return Jwts.builder()
                .setSubject(String.valueOf(userId))
                .claim("username", username)
                .claim("type", type == null ? "access" : type)
                .setIssuedAt(now)
                .setExpiration(new Date(now.getTime() + expireMinutes * 60_000))
                .signWith(key, SignatureAlgorithm.HS256)
                .compact();
    }

    /** 解析并校验；无效/过期抛异常由调用方转 401 */
    public Claims parse(String token) {
        return Jwts.parserBuilder().setSigningKey(key).build()
                .parseClaimsJws(token).getBody();
    }
}
