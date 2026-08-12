package com.grape.admin.service;

import com.grape.admin.common.BizException;
import com.grape.admin.common.NotFoundException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.Base64;

/**
 * 凭据签发/吊销——契约对齐 grape src/api/routes/runtime.py _auth_credential：
 * token 以 rta_ 开头；落库 token_hash = sha256(token) 小写 hex；status ∈ {active, revoked}。
 * 明文 token 仅在签发响应中出现一次。
 */
@Service
public class RuntimeCredentialService {

    private final JdbcTemplate jdbc;
    private final AdminAuditService auditService;
    private final SecureRandom secureRandom = new SecureRandom();

    public RuntimeCredentialService(JdbcTemplate jdbc, AdminAuditService auditService) {
        this.jdbc = jdbc;
        this.auditService = auditService;
    }

    public String issue(String credentialName, String operator) {
        if (credentialName == null || credentialName.isBlank() || credentialName.length() > 64) {
            throw new BizException("凭据名称不能为空且不超过 64 字符");
        }
        Integer exists = jdbc.queryForObject(
                "SELECT COUNT(*) FROM runtime_credentials WHERE name = ?", Integer.class, credentialName);
        if (exists != null && exists > 0) {
            throw new BizException("凭据名称已存在: " + credentialName);
        }
        byte[] raw = new byte[32];
        secureRandom.nextBytes(raw);
        String token = "rta_" + Base64.getUrlEncoder().withoutPadding().encodeToString(raw);
        String hash = sha256Hex(token);
        jdbc.update("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status, created_at) "
                        + "VALUES (?, ?, ?, 'user', 'active', NOW())",
                hash, credentialName, credentialName);
        auditService.record(null, operator, "credential.issue", "credential:" + credentialName, "");
        return token;
    }

    public void revoke(Integer credentialId, String operator) {
        int n = jdbc.update("UPDATE runtime_credentials SET status = 'revoked' WHERE id = ? AND status = 'active'",
                credentialId);
        if (n == 0) {
            throw new NotFoundException("凭据不存在或已吊销: " + credentialId);
        }
        auditService.record(null, operator, "credential.revoke", "credential:" + credentialId, "");
    }

    static String sha256Hex(String token) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(token.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (byte b : digest) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 不可用", e);
        }
    }
}
