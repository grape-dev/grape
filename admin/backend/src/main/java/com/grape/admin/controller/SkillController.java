package com.grape.admin.controller;

import com.grape.admin.config.AuthContext;
import com.grape.admin.service.SkillService;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestPart;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.multipart.MultipartFile;

import javax.servlet.http.HttpServletRequest;
import java.io.IOException;
import java.util.Map;

/**
 * Skill 管理（技能库，直读共享库 skill 表；仓库/个人技能上传，校验规则与主项目 grape 一致）。
 */
@RestController
@RequestMapping("/api/admin/skills")
public class SkillController {

    private final SkillService skillService;

    public SkillController(SkillService skillService) {
        this.skillService = skillService;
    }

    @GetMapping
    public Map<String, Object> list(@RequestParam(required = false) String keyword,
                                    @RequestParam(required = false) String agentType,
                                    @RequestParam(required = false) String scope,
                                    @RequestParam(defaultValue = "0") int page,
                                    @RequestParam(defaultValue = "10") int size) {
        return skillService.listSkills(keyword, agentType, scope, page, size);
    }

    @GetMapping("/{id}/content")
    public Map<String, Object> content(@PathVariable Integer id) {
        return skillService.getSkillContent(id);
    }

    @PostMapping(value = "/upload", consumes = MediaType.MULTIPART_FORM_DATA_VALUE)
    public Map<String, Object> upload(@RequestPart("file") MultipartFile file,
                                      @RequestParam("scope") String scope,
                                      @RequestParam(value = "repo", required = false) String repo,
                                      @RequestParam("agentType") String agentType,
                                      HttpServletRequest request) throws IOException {
        String username = AuthContext.username(request);
        try (var in = file.getInputStream()) {
            return skillService.uploadSkill(in, file.getSize(), file.getOriginalFilename(),
                    scope, repo, agentType, username);
        }
    }

    @GetMapping("/template")
    public ResponseEntity<byte[]> template() {
        byte[] bytes = skillService.buildTemplateZip();
        return ResponseEntity.ok()
                .header(HttpHeaders.CONTENT_DISPOSITION, "attachment; filename=\"skill-template.zip\"")
                .contentType(MediaType.APPLICATION_OCTET_STREAM)
                .body(bytes);
    }
}
