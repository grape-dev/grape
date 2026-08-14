package com.grape.admin.controller;

import com.grape.admin.config.AuthContext;
import com.grape.admin.service.MenuService;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import javax.servlet.http.HttpServletRequest;
import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/admin")
public class MenuController {

    private final MenuService menuService;

    public MenuController(MenuService menuService) {
        this.menuService = menuService;
    }

    @GetMapping("/menus")
    public List<Map<String, Object>> menus(HttpServletRequest request) {
        return menuService.menuTreeForUser(AuthContext.userId(request));
    }
}
