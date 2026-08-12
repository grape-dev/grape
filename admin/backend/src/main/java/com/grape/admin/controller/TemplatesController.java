package com.grape.admin.controller;

import com.grape.admin.service.TemplatesService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
@RequestMapping("/api/admin/templates")
public class TemplatesController {

    private final TemplatesService templatesService;

    public TemplatesController(TemplatesService templatesService) {
        this.templatesService = templatesService;
    }

    @GetMapping
    public Map<String, Object> list() {
        return templatesService.listTemplates();
    }
}
