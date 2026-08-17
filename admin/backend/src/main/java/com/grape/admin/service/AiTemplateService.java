package com.grape.admin.service;

import com.grape.admin.common.BizException;
import com.grape.admin.common.NotFoundException;
import com.grape.admin.entity.AdminAiTemplate;
import com.grape.admin.repository.AdminAiTemplateRepository;
import org.springframework.stereotype.Service;

import javax.transaction.Transactional;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.stream.Collectors;

@Service
public class AiTemplateService {

    private static final int MAX_TEMPLATES = 50;   // 与 VOD 一致：自定义模板上限
    private static final int MAX_NAME_LEN = 50;
    private static final int MAX_CONTENT_LEN = 600;

    private final AdminAiTemplateRepository templateRepository;

    public AiTemplateService(AdminAiTemplateRepository templateRepository) {
        this.templateRepository = templateRepository;
    }

    /** 当前用户的模板列表（含总数上限提示） */
    public Map<String, Object> list(String username) {
        List<AdminAiTemplate> list = templateRepository.findByCreatedByAndDeletedOrderByIdDesc(username, 0);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("templates", list.stream().map(this::toMap).collect(Collectors.toList()));
        result.put("count", list.size());
        result.put("max", MAX_TEMPLATES);
        return result;
    }

    /** 保存模板：每用户上限 50，名称/内容长度校验，同名去重 */
    @Transactional
    public Map<String, Object> save(String username, String name, String content) {
        if (name == null || name.trim().isEmpty()) throw new BizException("模板名称不能为空");
        if (name.trim().length() > MAX_NAME_LEN) throw new BizException("模板名称不能超过 " + MAX_NAME_LEN + " 字");
        if (content == null || content.trim().isEmpty()) throw new BizException("模板内容不能为空");
        if (content.trim().length() > MAX_CONTENT_LEN) throw new BizException("模板内容不能超过 " + MAX_CONTENT_LEN + " 字");
        long count = templateRepository.countByCreatedByAndDeleted(username, 0);
        if (count >= MAX_TEMPLATES) throw new BizException("自定义模板最多 " + MAX_TEMPLATES + " 个，请先删除旧模板");
        String cleanName = name.trim();
        if (templateRepository.existsByCreatedByAndTemplateNameAndDeleted(username, cleanName, 0)) {
            throw new BizException("已存在同名模板：" + cleanName);
        }
        // 内容查重（对齐 VOD——防绕过前端存重复内容）
        String cleanContent = content.trim();
        if (templateRepository.existsByCreatedByAndTemplateContentAndDeleted(username, cleanContent, 0)) {
            throw new BizException("已存在相同内容的模板");
        }
        AdminAiTemplate t = new AdminAiTemplate();
        t.setTemplateName(cleanName);
        t.setTemplateContent(cleanContent);
        t.setCreatedBy(username);
        templateRepository.save(t);
        return toMap(t);
    }

    /** 删除模板（软删，仅本人） */
    @Transactional
    public void delete(String username, Long id) {
        AdminAiTemplate t = templateRepository.findById(id)
                .orElseThrow(() -> new NotFoundException("模板不存在"));
        if (!username.equals(t.getCreatedBy())) throw new BizException("只能删除自己的模板");
        t.setDeleted(1);
        templateRepository.save(t);
    }

    private Map<String, Object> toMap(AdminAiTemplate t) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", t.getId());
        m.put("name", t.getTemplateName());
        m.put("content", t.getTemplateContent());
        m.put("createdAt", String.valueOf(t.getCreatedAt()));
        return m;
    }
}
