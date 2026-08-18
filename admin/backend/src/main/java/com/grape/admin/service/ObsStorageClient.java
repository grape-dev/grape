package com.grape.admin.service;

import com.obs.services.ObsClient;
import com.obs.services.model.DeleteObjectsRequest;
import com.obs.services.model.DeleteObjectsResult;
import com.obs.services.model.KeyAndVersion;
import com.obs.services.model.ObjectMetadata;
import com.obs.services.model.PutObjectRequest;
import com.obs.services.model.ListObjectsRequest;
import com.obs.services.model.ObjectListing;
import com.obs.services.model.ObsObject;
import com.obs.services.model.HttpMethodEnum;
import com.obs.services.model.TemporarySignatureRequest;
import com.obs.services.model.TemporarySignatureResponse;
import org.apache.commons.compress.archivers.zip.ZipArchiveEntry;
import org.apache.commons.compress.archivers.zip.ZipArchiveOutputStream;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.annotation.PostConstruct;
import javax.annotation.PreDestroy;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermission;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * 对象存储 OBS 客户端（obs-skill-storage-design.md §3 N12~N15）。
 *
 * 凭据边界：仅对配置桶内对象操作（put/copy/delete/list/签临时 URL），
 * 不做任何桶级/账号级越权操作。AK/SK 仅经环境变量注入。
 *
 * 与主服务 Python 侧对象键规范一致：
 *   {PREFIX}/skills/{custom|repo|official}/{owner}/{skill_name}/v{n}/{skill_name}-{rand8}.zip
 */
@Component
public class ObsStorageClient {

    private static final Logger log = LoggerFactory.getLogger(ObsStorageClient.class);
    private static final SecureRandom RANDOM = new SecureRandom();

    private final boolean enabled;
    private final String endpoint;
    private final String bucket;
    private final String accessKey;
    private final String secretKey;
    private final String prefix;
    private final int urlExpires;

    private ObsClient client;

    public ObsStorageClient(
            @Value("${app.obs.enabled:false}") boolean enabled,
            @Value("${app.obs.endpoint:}") String endpoint,
            @Value("${app.obs.bucket:}") String bucket,
            @Value("${app.obs.access-key:}") String accessKey,
            @Value("${app.obs.secret-key:}") String secretKey,
            @Value("${app.obs.prefix:grape/prod}") String prefix,
            @Value("${app.obs.url-expires:300}") int urlExpires) {
        this.enabled = enabled && bucket != null && !bucket.isBlank()
                && accessKey != null && !accessKey.isBlank()
                && secretKey != null && !secretKey.isBlank();
        this.endpoint = endpoint;
        this.bucket = bucket;
        this.accessKey = accessKey;
        this.secretKey = secretKey;
        this.prefix = (prefix == null ? "" : prefix).replaceAll("/+$", "");
        this.urlExpires = urlExpires > 0 ? urlExpires : 300;
    }

    @PostConstruct
    public void init() {
        if (enabled) {
            client = new ObsClient(accessKey, secretKey, endpoint);
            log.info("ObsStorageClient ready: bucket={} prefix={}", bucket, prefix);
        } else {
            log.info("ObsStorageClient disabled (OBS_ENABLED=false or config incomplete)");
        }
    }

    @PreDestroy
    public void close() {
        if (client != null) {
            try {
                client.close();
            } catch (Exception e) {
                log.warn("ObsStorageClient close failed: {}", e.getMessage());
            }
        }
    }

    public boolean enabled() {
        return enabled;
    }

    public String bucket() {
        return bucket;
    }

    public int urlExpires() {
        return urlExpires;
    }

    // ---- 对象键 ----

    /** 生成技能 zip 对象键（与主服务 skill_key 同规则；v2.4 固定 key 覆盖模型）。
     *  {PREFIX}/skills/{scope}/{owner}/{skillName}/{skillName}.zip（无版本段/随机段） */
    public String skillKey(String scope, String owner, String skillName, int version) {
        String seg = "user".equals(scope) ? "custom" : scope;
        String ownerPath = (owner == null || owner.isBlank()) ? "" : owner + "/";
        return prefix + "/skills/" + seg + "/" + ownerPath + skillName
                + "/" + skillName + ".zip";
    }

    /** 判断 skill_path 是否为 OBS 对象键（与主服务 get_skill_object_key 同判据；v2.4 固定 key）。 */
    public boolean isObjectKey(String skillPath) {
        return skillPath != null && skillPath.contains("/skills/") && skillPath.endsWith(".zip");
    }

    private String full(String key) {
        return key;
    }

    // ---- 写入 ----

    public void saveBytes(String key, byte[] data) {
        PutObjectRequest req = new PutObjectRequest(bucket, full(key), new ByteArrayInputStream(data));
        ObjectMetadata meta = new ObjectMetadata();
        meta.setContentLength((long) data.length);
        req.setMetadata(meta);
        client.putObject(req);
    }

    // ---- 读取 ----

    public byte[] readBytes(String key) {
        ObsObject obj = client.getObject(bucket, full(key));
        try (InputStream in = obj.getObjectContent();
             ByteArrayOutputStream bos = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) {
                bos.write(buf, 0, n);
            }
            return bos.toByteArray();
        } catch (IOException e) {
            throw new RuntimeException("OBS 读取失败: " + key + " -> " + e.getMessage(), e);
        }
    }

    // ---- 同桶复制（转换/提升；不跨桶） ----

    public boolean copyObject(String srcKey, String dstKey) {
        try {
            client.copyObject(bucket, full(srcKey), bucket, full(dstKey));
            return true;
        } catch (Exception e) {
            log.warn("OBS copyObject failed: {} -> {}: {}", srcKey, dstKey, e.getMessage());
            return false;
        }
    }

    // ---- 删除（单对象/按前缀；v2.4 固定 key 模型删除 = 单对象） ----

    public boolean deleteObject(String key) {
        try {
            client.deleteObject(bucket, full(key));
            return true;
        } catch (Exception e) {
            log.warn("OBS deleteObject failed: {}: {}", key, e.getMessage());
            return false;
        }
    }

    public int deletePrefix(String keyPrefix) {
        try {
            ListObjectsRequest req = new ListObjectsRequest(bucket);
            req.setPrefix(full(keyPrefix));
            ObjectListing listing = client.listObjects(req);
            List<KeyAndVersion> keys = new ArrayList<>();
            for (ObsObject o : listing.getObjects()) {
                keys.add(new KeyAndVersion(o.getObjectKey()));
            }
            if (keys.isEmpty()) {
                return 0;
            }
            DeleteObjectsRequest del = new DeleteObjectsRequest(bucket);
            del.setKeyAndVersions(keys.toArray(new KeyAndVersion[0]));
            DeleteObjectsResult res = client.deleteObjects(del);
            return res.getDeletedObjectResults() == null ? keys.size() : res.getDeletedObjectResults().size();
        } catch (Exception e) {
            log.warn("OBS deletePrefix failed: {}: {}", keyPrefix, e.getMessage());
            return 0;
        }
    }

    // ---- 临时签名 URL（仅 GET） ----

    public String temporaryUrl(String key) {
        try {
            TemporarySignatureRequest req = new TemporarySignatureRequest();
            req.setMethod(HttpMethodEnum.GET);
            req.setBucketName(bucket);
            req.setObjectKey(full(key));
            req.setExpires((long) urlExpires);
            TemporarySignatureResponse resp = client.createTemporarySignature(req);
            return resp.getSignedUrl();
        } catch (Exception e) {
            throw new RuntimeException("OBS 临时 URL 签发失败: " + key, e);
        }
    }

    // ---- zip 工具 ----

    /** 规范化重打包技能目录：根 = {skill_name}/，保留权限位（与主服务 _normalize_skill_zip 一致）。
     *  用 commons-compress 写 zip（标准库 ZipEntry 不支持 external attrs）。 */
    public byte[] normalizeSkillZip(Path skillDir, String skillName) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        try (ZipArchiveOutputStream zos = new ZipArchiveOutputStream(bos)) {
            try (var walk = Files.walk(skillDir)) {
                List<Path> files = walk.filter(Files::isRegularFile).collect(Collectors.toList());
                for (Path f : files) {
                    String rel = skillDir.relativize(f).toString().replace("\\", "/");
                    String arc = skillName + "/" + rel;
                    ZipArchiveEntry entry = new ZipArchiveEntry(arc);
                    entry.setTime(f.toFile().lastModified());
                    Set<PosixFilePermission> perms = null;
                    try {
                        perms = Files.getPosixFilePermissions(f);
                    } catch (Exception ignored) {
                        // 非 POSIX 环境忽略
                    }
                    if (perms != null) {
                        entry.setUnixMode(posixMode(perms));
                    }
                    zos.putArchiveEntry(entry);
                    Files.copy(f, zos);
                    zos.closeArchiveEntry();
                }
            }
        }
        return bos.toByteArray();
    }

    /** 从技能 zip 提取 SKILL.md 文本（zip 根 = {skill_name}/）。 */
    public String extractSkillMd(byte[] zipBytes, String skillName) {
        try (ZipInputStream zis = new ZipInputStream(new ByteArrayInputStream(zipBytes))) {
            ZipEntry e;
            while ((e = zis.getNextEntry()) != null) {
                if (e.isDirectory()) {
                    continue;
                }
                String name = e.getName();
                String[] parts = name.split("/", 2);
                if (parts.length == 2 && parts[0].equals(skillName)
                        && "SKILL.md".equalsIgnoreCase(parts[1])) {
                    ByteArrayOutputStream bos = new ByteArrayOutputStream();
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = zis.read(buf)) != -1) {
                        bos.write(buf, 0, n);
                    }
                    return bos.toString(StandardCharsets.UTF_8);
                }
            }
        } catch (IOException e) {
            log.warn("extractSkillMd failed: {}", e.getMessage());
        }
        return "";
    }

    private static int posixMode(Set<PosixFilePermission> perms) {
        int mode = 0;
        if (perms.contains(PosixFilePermission.OWNER_READ)) mode |= 0400;
        if (perms.contains(PosixFilePermission.OWNER_WRITE)) mode |= 0200;
        if (perms.contains(PosixFilePermission.OWNER_EXECUTE)) mode |= 0100;
        if (perms.contains(PosixFilePermission.GROUP_READ)) mode |= 0040;
        if (perms.contains(PosixFilePermission.GROUP_WRITE)) mode |= 0020;
        if (perms.contains(PosixFilePermission.GROUP_EXECUTE)) mode |= 0010;
        if (perms.contains(PosixFilePermission.OTHERS_READ)) mode |= 0004;
        if (perms.contains(PosixFilePermission.OTHERS_WRITE)) mode |= 0002;
        if (perms.contains(PosixFilePermission.OTHERS_EXECUTE)) mode |= 0001;
        return mode;
    }
}
