package com.grape.admin.controller;

import com.grape.admin.service.SkillService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

/**
 * Skill 管理（技能库，直读共享库 skill 表）。
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
}
