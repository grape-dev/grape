package com.grape.admin.config;

import io.jsonwebtoken.JwtException;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class JwtUtilTest {

    private JwtUtil jwtUtil;

    @BeforeEach
    void setUp() {
        jwtUtil = new JwtUtil();
        jwtUtil.expireMinutes = 120;
        jwtUtil.secret = "test-secret-0123456789-0123456789-0123456789";
        jwtUtil.init();
    }

    @Test
    void generateAndParse() {
        String token = jwtUtil.generateToken(1L, "admin");
        var claims = jwtUtil.parse(token);
        assertEquals("1", claims.getSubject());
        assertEquals("admin", claims.get("username", String.class));
    }

    @Test
    void tamperedTokenRejected() {
        String token = jwtUtil.generateToken(1L, "admin");
        String tampered = token.substring(0, token.length() - 2) + "xx";
        assertThrows(JwtException.class, () -> jwtUtil.parse(tampered));
    }

    @Test
    void expiredTokenRejected() {
        JwtUtil shortLived = new JwtUtil();
        shortLived.secret = jwtUtil.secret;
        shortLived.expireMinutes = -1;   // 已过期
        shortLived.init();
        String token = shortLived.generateToken(1L, "admin");
        assertThrows(JwtException.class, () -> jwtUtil.parse(token));
    }

    @Test
    void blankSecretForbidden() {
        JwtUtil bad = new JwtUtil();
        bad.secret = "";
        assertThrows(IllegalStateException.class, bad::init);
    }
}
