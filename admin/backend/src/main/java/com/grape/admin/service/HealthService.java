package com.grape.admin.service;

import org.springframework.stereotype.Component;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;

/**
 * 健康状态派生——移植 grape src/runtime/health.py 规则：
 * status=online → online；无 last_seen → offline；
 * 失联 <5min → recently_lost；>6d → about_to_gc；其余 offline。
 * 时间解析：ISO-8601，naive 视为 UTC（与 grape 一致）。
 */
@Component
public class HealthService {

    private static final long RECENTLY_LOST_MS = 5 * 60 * 1000L;
    private static final long ABOUT_TO_GC_MS = 6 * 24 * 3600 * 1000L;

    /** 引擎健康（agent_runtimes.status 权威） */
    public String engineHealth(String status, String lastSeenAt) {
        if ("online".equals(status)) {
            return "online";
        }
        return deriveOffline(lastSeenAt);
    }

    /** 设备健康（凭据无 online 状态，按 last_seen_at 推导） */
    public String deviceHealth(String credentialStatus, String lastSeenAt) {
        if (credentialStatus != null && !"active".equals(credentialStatus)) {
            return "revoked";
        }
        if ("active".equals(credentialStatus)) {
            return deriveOnline(lastSeenAt);
        }
        return deriveOffline(lastSeenAt);
    }

    private String deriveOnline(String lastSeenAt) {
        Long ts = parseTs(lastSeenAt);
        if (ts == null) {
            return "offline";
        }
        long offlineFor = System.currentTimeMillis() - ts;
        if (offlineFor < RECENTLY_LOST_MS) {
            return "online";
        }
        if (offlineFor > ABOUT_TO_GC_MS) {
            return "about_to_gc";
        }
        return "offline";
    }

    private String deriveOffline(String lastSeenAt) {
        Long ts = parseTs(lastSeenAt);
        if (ts == null) {
            return "offline";
        }
        long offlineFor = System.currentTimeMillis() - ts;
        if (offlineFor < RECENTLY_LOST_MS) {
            return "recently_lost";
        }
        if (offlineFor > ABOUT_TO_GC_MS) {
            return "about_to_gc";
        }
        return "offline";
    }

    private Long parseTs(String s) {
        if (s == null || s.isBlank()) {
            return null;
        }
        try {
            OffsetDateTime odt = OffsetDateTime.parse(s, DateTimeFormatter.ISO_OFFSET_DATE_TIME);
            return odt.toInstant().toEpochMilli();
        } catch (Exception e) {
            try {
                Instant i = Instant.parse(s);
                return i.toEpochMilli();
            } catch (Exception e2) {
                try {
                    OffsetDateTime naive = OffsetDateTime.parse(s, DateTimeFormatter.ISO_LOCAL_DATE_TIME)
                            .withOffsetSameInstant(ZoneOffset.UTC);
                    return naive.toInstant().toEpochMilli();
                } catch (Exception e3) {
                    return null;
                }
            }
        }
    }
}
