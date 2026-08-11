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

    public String issue(String deviceName, String operator) {
        if (deviceName == null || deviceName.isBlank() || deviceName.length() > 64) {
            throw new BizException("设备名不能为空且不超过 64 字符");
        }
        Integer exists = jdbc.queryForObject(
                "SELECT COUNT(*) FROM runtime_credentials WHERE name = ?", Integer.class, deviceName);
        if (exists != null && exists > 0) {
            throw new BizException("设备名已存在: " + deviceName);
        }
        byte[] raw = new byte[32];
        secureRandom.nextBytes(raw);
        String token = "rta_" + Base64.getUrlEncoder().withoutPadding().encodeToString(raw);
        String hash = sha256Hex(token);
        jdbc.update("INSERT INTO runtime_credentials (token_hash, name, device_name, scope, status, created_at) "
                        + "VALUES (?, ?, ?, 'user', 'active', NOW())",
                hash, deviceName, deviceName);
        auditService.record(null, operator, "credential.issue", "device:" + deviceName, "");
        return token;
    }

    public void revoke(Integer credentialId, String operator) {
        int n = jdbc.update("UPDATE runtime_credentials SET status = 'revoked' WHERE id = ? AND status = 'active'",
                credentialId);
        if (n == 0) {
            throw new NotFoundException("设备不存在或已吊销: " + credentialId);
        }
        auditService.record(null, operator, "credential.revoke", "device:" + credentialId, "");
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
